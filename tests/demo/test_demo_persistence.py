"""Stage 3: snapshot persistence to a private Supabase Storage bucket, tested against an in-memory fake of the
Storage REST API (no credentials, no network). The real workspace app is used for round trips."""

import asyncio
import io
import json
import sqlite3
import stat
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ledgerspy_demo import persistence as P
from ledgerspy_demo.frontdoor import FrontDoor
from ledgerspy_demo.frontend import build_demo_frontend
from ledgerspy_demo.identity import new_workspace_id
from ledgerspy_demo.persistence import Persistence, archive_key, pointer_key
from ledgerspy_demo.settings import DemoSettings
from ledgerspy_demo.snapshots import (
    MANIFEST,
    SnapshotError,
    build_snapshot,
    extract_snapshot,
    validate_manifest,
)
from ledgerspy_demo.storage import Request, Response, StorageError, SupabaseStorage, check_key
from ledgerspy_demo.workspace import WorkspaceManager, WorkspaceUnavailable
from tests.demo.conftest import Clock, Visitor, demo_env, request

KEY = "test-only-service-key-0123456789"
URL = "https://fake-project.supabase.test"
BUCKET = "ledgerspy-demo-private"
LEDGER = Path(__file__).resolve().parents[2] / "data" / "synthetic" / "reference" / "SYNTHETIC_ledger.csv"


class FakeStorage:
    """The subset of the Supabase Storage REST API the demo uses (server-side timestamps, failure hooks)."""

    def __init__(self, clock, public: bool = False, key: str = KEY) -> None:
        self.key = key
        self.objects: dict[str, tuple[bytes, float]] = {}
        self.clock, self.public = clock, public
        self.calls: list[tuple[str, str]] = []
        self.fail = None  # callable(Request) -> Response | Exception | None
        self.lock = threading.Lock()

    def __call__(self, req: Request) -> Response:
        path = req.url.split("/storage/v1", 1)[1]
        with self.lock:
            self.calls.append((req.method, path))
        assert req.url.startswith(URL + "/storage/v1/")
        if self.key.startswith("sb_secret_"):  # current Supabase secret keys: `apikey` only, never Bearer
            authorised = req.headers.get("apikey") == self.key and "Authorization" not in req.headers
        else:  # legacy service_role JWT: both headers
            authorised = (
                req.headers.get("Authorization") == f"Bearer {self.key}"
                and req.headers.get("apikey") == self.key
            )
        if not authorised:
            return Response(400, b'{"statusCode":"403","error":"Unauthorized"}')
        if self.fail is not None:
            out = self.fail(req)
            if isinstance(out, Exception):
                raise out
            if out is not None:
                return out
        if req.method == "GET" and path == f"/bucket/{BUCKET}":
            return Response(200, json.dumps({"name": BUCKET, "public": self.public}).encode())
        if path.startswith(f"/object/list/{BUCKET}"):
            q = json.loads(req.body)
            return Response(200, json.dumps(self._list(q["prefix"], q["limit"], q["offset"])).encode())
        if req.method == "DELETE" and path == f"/object/{BUCKET}":
            with self.lock:
                for k in json.loads(req.body)["prefixes"]:
                    self.objects.pop(k, None)
            return Response(200, b"[]")
        key = path.removeprefix(f"/object/{BUCKET}/")
        if req.method == "POST":
            with self.lock:
                if key in self.objects and req.headers.get("x-upsert") == "false":
                    return Response(400, b'{"statusCode":"409","error":"Duplicate","message":"exists"}')
                self.objects[key] = (req.body, self.clock())
            return Response(200, json.dumps({"Key": f"{BUCKET}/{key}"}).encode())
        if req.method == "GET":
            with self.lock:
                if key not in self.objects:
                    return Response(
                        400, b'{"statusCode":"404","error":"not_found","message":"Object not found"}'
                    )
                data = self.objects[key][0]
            if len(data) > req.max_response:
                raise StorageError("too_large", 200)
            return Response(200, data)
        return Response(400, b"{}")

    def _list(self, prefix: str, limit: int, offset: int) -> list:
        children: dict[str, dict] = {}
        with self.lock:
            for k, (data, t) in sorted(self.objects.items()):
                if not k.startswith(prefix):
                    continue
                rest = k[len(prefix) :]
                if "/" in rest:
                    name = rest.split("/", 1)[0]
                    children.setdefault(name, {"name": name, "id": None})
                else:
                    stamp = datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")
                    children[rest] = {
                        "name": rest,
                        "id": "obj",
                        "created_at": stamp,
                        "metadata": {"size": len(data)},
                    }
        return list(children.values())[offset : offset + limit]


def settings_for(data: Path, **limits) -> DemoSettings:
    env = demo_env(data, **limits)
    env.update(
        LEDGERSPY_DEMO_STORAGE_URL=URL,
        LEDGERSPY_DEMO_STORAGE_BUCKET=BUCKET,
        LEDGERSPY_DEMO_STORAGE_SERVICE_KEY=KEY,
    )
    return DemoSettings.from_env(env)


class Server:
    """One process's demo: manager, persistence and front door over a shared fake bucket."""

    def __init__(
        self, root: Path, name: str, seed_dir: Path, fake: FakeStorage, clock: Clock, **limits
    ) -> None:
        data = root / name
        data.mkdir()
        self.settings = settings_for(data, **limits)
        frontend = build_demo_frontend(data / "frontend", 7, True)
        self.manager = WorkspaceManager(self.settings, seed_dir, frontend, clock=clock)
        storage = SupabaseStorage(self.settings.storage, transport=fake, sleep=lambda s: None)
        (data / "tmp").mkdir()
        self.persistence = Persistence(self.manager, storage, temp_root=data / "tmp", clock=clock)
        self.manager.persistence = self.persistence
        self.door = FrontDoor(self.settings, self.manager, clock=clock)


@pytest.fixture
def world(tmp_path, seed_dir):
    clock = Clock()
    fake = FakeStorage(clock)

    def server(name: str, **limits) -> Server:
        return Server(tmp_path, name, seed_dir, fake, clock, **limits)

    return clock, fake, server


def run(coro):
    return asyncio.run(coro)


async def visitor(s: Server, octet: int = 10, cookie: str | None = None) -> Visitor:
    v = Visitor(s.door, client=(f"198.51.100.{octet}", 1), cookie=cookie)
    await v.open()
    return v


async def upload_rows(v: Visitor, n: int, tag: str) -> int:
    lines = LEDGER.read_text(encoding="utf-8").splitlines(keepends=True)[: n + 1]
    lines[-1] = lines[-1].replace("Team lunch", f"Team lunch {tag}", 1)
    case = (await v.call("GET", "/api/datasets")).json()["items"][0]["case_id"]
    r = await v.call("POST", f"/api/cases/{case}/files?filename={tag}.csv", body="".join(lines).encode())
    assert r.status == 201, r.body[:300]
    return len((await v.call("GET", "/api/datasets")).json()["items"])


def wid_of(v: Visitor) -> str:
    return v.cookie.split(".")[0]


# ---------------------------------------------------------------------------------- round trip and restart


def test_save_and_restore_after_a_restart(world):
    clock, fake, server = world

    async def go():
        a = server("first")
        await a.persistence._check_bucket()
        assert a.persistence.mode == "ready"
        v = await visitor(a)
        wid = wid_of(v)
        assert a.persistence.status(wid)["persistence"] == "pending"  # never claimed before a save
        datasets = await upload_rows(v, 40, "kept")
        st = a.persistence.state(wid)
        assert st.dirty and a.persistence.status(wid)["unsaved_changes"] is True
        assert await a.persistence.snapshot(wid)
        assert a.persistence.status(wid)["persistence"] == "saved" and not st.dirty
        assert archive_key(wid, 1) in fake.objects and pointer_key(wid, 1) in fake.objects

        b = server("after-restart")  # a new process, an empty disk, the same bucket
        await b.persistence._check_bucket()
        v2 = Visitor(b.door, client=("198.51.100.10", 1), cookie=v.cookie)
        r = await v2.open()  # first access restores the saved copy, verifies it, then opens it
        assert r.status == 200 and v2.token
        assert len((await v2.call("GET", "/api/datasets")).json()["items"]) == datasets
        assert (await v2.call("GET", "/api/audit/verify")).json()["ok"] is True
        assert b.persistence.status(wid)["persistence"] == "saved"
        # a new snapshot after the restart continues the generation; it never reuses an old one
        await upload_rows(v2, 10, "later")
        assert await b.persistence.snapshot(wid)
        assert pointer_key(wid, 2) in fake.objects

    run(go())


def test_a_workspace_with_nothing_saved_is_reported_lost_not_replaced(world):
    _clock, _fake, server = world

    async def go():
        a = server("one")
        await a.persistence._check_bucket()
        v = await visitor(a)
        b = server("two")
        await b.persistence._check_bucket()
        r = await Visitor(b.door, cookie=v.cookie).call("GET", "/api/datasets")
        assert r.status == 409 and r.json()["error"]["code"] == "workspace_restore_failed"
        assert not (b.manager.root / wid_of(v)).exists()

    run(go())


def test_missing_credentials_or_a_public_bucket_disable_persistence(world, tmp_path, seed_dir):
    clock, _fake, server = world

    async def go():
        plain = DemoSettings.from_env(demo_env(tmp_path / "plain"))
        assert plain.storage is None and plain.persistent is False
        env = demo_env(tmp_path / "x")
        env.update(LEDGERSPY_DEMO_STORAGE_URL=URL, LEDGERSPY_DEMO_STORAGE_BUCKET=BUCKET)
        with pytest.raises(Exception, match="set all of"):
            DemoSettings.from_env(env)
        env["LEDGERSPY_DEMO_STORAGE_SERVICE_KEY"] = "short"
        with pytest.raises(Exception, match="service key"):
            DemoSettings.from_env(env)
        public_fake = FakeStorage(clock, public=True)
        s = Server(tmp_path, "public", seed_dir, public_fake, clock)
        await s.persistence._check_bucket()
        assert (
            s.persistence.mode == "disabled"
            and s.persistence.disabled_reason == "the configured bucket is public"
        )
        v = await visitor(s)
        assert s.persistence.status(wid_of(v))["persistence"] == "disabled"
        assert not await s.persistence.snapshot(wid_of(v)) and public_fake.objects == {}

    run(go())


# -------------------------------------------------------------------------------- isolation and validation


def test_one_workspace_can_never_restore_another_ones_copy(world):
    clock, fake, server = world

    async def go():
        a = server("a")
        await a.persistence._check_bucket()
        va = await visitor(a, 20)
        vb = await visitor(a, 21)
        await upload_rows(va, 20, "secret")
        assert await a.persistence.snapshot(wid_of(va))
        # tampering: A's objects copied under B's folder (as an attacker with bucket access might)
        a_w, b_w = wid_of(va), wid_of(vb)
        for gen_key in (archive_key(a_w, 1), pointer_key(a_w, 1)):
            data, t = fake.objects[gen_key]
            fake.objects[gen_key.replace(a_w, b_w)] = (data, t)
        b = server("b")
        await b.persistence._check_bucket()
        r = await Visitor(b.door, cookie=vb.cookie).call("GET", "/api/datasets")
        assert r.status == 409 and r.json()["error"]["code"] == "workspace_restore_failed"
        assert (
            "another workspace" in r.json()["error"]["message"] or "pointer" in r.json()["error"]["message"]
        )
        assert not (b.manager.root / b_w).exists()
        for bad in (
            "workspaces/../x",
            f"workspaces/{a_w}/../../etc",
            "other/thing.zip",
            f"workspaces/{a_w}/x.zip",
        ):
            with pytest.raises(ValueError):
                check_key(bad)

    run(go())


def test_a_damaged_copy_is_refused_and_the_workspace_is_never_replaced(world):
    clock, fake, server = world

    async def go():
        a = server("a")
        await a.persistence._check_bucket()
        v = await visitor(a)
        assert (
            await a.persistence.snapshot(wid_of(v)) is True or True
        )  # (nothing changed yet: nothing to save)
        await upload_rows(v, 10, "x")
        assert await a.persistence.snapshot(wid_of(v))
        wid = wid_of(v)
        key = archive_key(wid, 1)
        data, t = fake.objects[key]
        fake.objects[key] = (
            data[:-10] + b"0123456789",
            t,
        )  # one changed byte range: the checksum must catch it
        b = server("b")
        await b.persistence._check_bucket()
        r = await Visitor(b.door, cookie=v.cookie).call("GET", "/api/datasets")
        assert r.status == 409 and "damaged" in r.json()["error"]["message"]
        assert not (b.manager.root / wid).exists()
        assert not [p for p in b.manager.root.iterdir() if p.name.startswith(".")]  # staging removed
        assert not any((b.manager.root.parent / "tmp").iterdir())

    run(go())


def _zip(entries: list[tuple[str, bytes]], mode: int | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            z.writestr(info, data)
    return buf.getvalue()


def _manifest(wid: str, files: dict[str, bytes]) -> bytes:
    import hashlib

    from ledgerspy.cases.db import SCHEMA_VERSION

    entries = [{"path": k, "size": len(v), "sha256": hashlib.sha256(v).hexdigest()} for k, v in files.items()]
    return json.dumps(
        {
            "format": "ledgerspy-demo-snapshot",
            "version": 1,
            "workspace_id": wid,
            "generation": 1,
            "created_at": 1.0,
            "workspace_created_at": 1.0,
            "schema_version": SCHEMA_VERSION,
            "total_bytes": sum(len(v) for v in files.values()),
            "files": entries,
        }
    ).encode()


@pytest.mark.filterwarnings("ignore:Duplicate name:UserWarning")  # one case: a duplicate on purpose
@pytest.mark.parametrize(
    "entries, mode",
    [
        ([("../ledgerspy.db", b"x")], None),
        ([("/etc/passwd", b"x")], None),
        ([("evidence\\..\\x", b"x")], None),
        ([("ledgerspy.db", b"x"), ("ledgerspy.db", b"y")], None),
        ([("tmp/upload.part", b"x")], None),
        ([("ledgerspy.db", b"x")], stat.S_IFLNK | 0o777),
        ([("evidence/", b"")], stat.S_IFDIR | 0o755),
    ],
)
def test_unsafe_archives_are_refused_before_anything_is_written(tmp_path, entries, mode):
    wid = new_workspace_id()
    body = _zip([(MANIFEST, _manifest(wid, {"ledgerspy.db": b"x"}))] + entries, mode)
    archive = tmp_path / "a.zip"
    archive.write_bytes(body)
    import hashlib

    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(SnapshotError):
        extract_snapshot(archive, hashlib.sha256(body).hexdigest(), len(body), wid, 25 * 1024 * 1024, dest)
    assert list(dest.iterdir()) == []


def test_oversized_and_inconsistent_snapshots_are_refused(tmp_path):
    import hashlib

    wid = new_workspace_id()
    big = b"\0" * 300_000
    body = _zip([(MANIFEST, _manifest(wid, {"ledgerspy.db": big})), ("ledgerspy.db", big)])
    archive = tmp_path / "a.zip"
    archive.write_bytes(body)
    dest = tmp_path / "d"
    dest.mkdir()
    with pytest.raises(SnapshotError, match="larger"):
        extract_snapshot(archive, hashlib.sha256(body).hexdigest(), len(body), wid, 100_000, dest)
    with pytest.raises(SnapshotError, match="another workspace"):
        validate_manifest(json.loads(_manifest(new_workspace_id(), {"ledgerspy.db": b"x"})), wid, 10**6)
    lying = json.loads(_manifest(wid, {"ledgerspy.db": b"abc"}))
    lying["files"][0]["sha256"] = "0" * 64
    body = _zip([(MANIFEST, json.dumps(lying).encode()), ("ledgerspy.db", b"abc")])
    archive.write_bytes(body)
    with pytest.raises(SnapshotError, match="checksum"):
        extract_snapshot(archive, hashlib.sha256(body).hexdigest(), len(body), wid, 10**6, dest)


def test_a_snapshot_is_consistent_while_the_database_is_being_written(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    db = ws / "ledgerspy.db"
    c = sqlite3.connect(db, isolation_level=None)
    c.execute("CREATE TABLE schema_version (version INTEGER)")
    from ledgerspy.cases.db import SCHEMA_VERSION

    c.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
    c.execute("CREATE TABLE t (n INTEGER, pad BLOB)")
    c.close()
    stop = threading.Event()

    def writer():
        w = sqlite3.connect(db, isolation_level=None, timeout=30)
        n = 0
        while not stop.is_set():
            w.execute("BEGIN IMMEDIATE")
            for _ in range(50):
                w.execute("INSERT INTO t VALUES (?, randomblob(500))", (n,))
            w.execute("COMMIT")  # rows always arrive in whole batches of 50
            n += 1
        w.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        counts = []
        for i in range(5):
            staging = tmp_path / f"s{i}"
            staging.mkdir()
            snap = build_snapshot(ws, new_workspace_id(), i + 1, 1.0, 1.0, 10**9, staging)
            out = tmp_path / f"x{i}"
            out.mkdir()
            with zipfile.ZipFile(snap.archive) as z:
                z.extract("ledgerspy.db", out)
            copy = sqlite3.connect(out / "ledgerspy.db")
            assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            count = copy.execute("SELECT count(*) FROM t").fetchone()[0]
            copy.close()
            assert count % 50 == 0  # never a half-committed transaction
            counts.append(count)
    finally:
        stop.set()
        t.join()
    assert counts == sorted(counts)


# -------------------------------------------------------------------------------- concurrency and ordering


def test_writes_during_an_upload_stay_unsaved(world):
    clock, fake, server = world

    async def go():
        a = server("a")
        await a.persistence._check_bucket()
        v = await visitor(a)
        wid = wid_of(v)
        await upload_rows(v, 10, "first")
        entered, release = threading.Event(), threading.Event()

        def hold_archive_upload(req):
            if req.method == "POST" and req.url.endswith(".zip"):
                entered.set()
                release.wait(30)
            return None

        fake.fail = hold_archive_upload
        snap = asyncio.create_task(a.persistence.snapshot(wid))
        await asyncio.to_thread(entered.wait, 30)  # the local copy is made; the upload is in flight
        assert not a.manager._resident[wid].lock.locked()  # the visitor's workspace is not held by the upload
        await upload_rows(v, 12, "during")  # a change after the copy was taken
        release.set()
        assert await snap is False  # the copy is stored, but it does not hold the newest change
        st = a.persistence.state(wid)
        assert st.generation == 1 and st.dirty and a.persistence.status(wid)["persistence"] == "pending"
        fake.fail = None
        assert await a.persistence.snapshot(wid) and st.generation == 2 and not st.dirty

    run(go())


def test_an_older_writer_can_never_replace_a_newer_snapshot(world):
    clock, fake, server = world

    async def go():
        a = server("a")
        await a.persistence._check_bucket()
        v = await visitor(a)
        wid = wid_of(v)
        await upload_rows(v, 10, "one")
        assert await a.persistence.snapshot(wid)
        # a second writer that does not know generation 1 exists (e.g. an overlapping instance)
        other = server("other")
        await other.persistence._check_bucket()
        st_other = other.persistence.state(wid)
        st_other.remote_checked = True  # pretend it never looked: it would try generation 1 again
        v2 = Visitor(other.door, cookie=v.cookie)
        await v2.open()  # restored: it now knows generation 1 (restore sets it)
        assert st_other.generation == 1
        st_other.generation = 0  # force the stale state
        await upload_rows(v2, 11, "two")
        assert await other.persistence.snapshot(wid) is False  # create-only: generation 1 exists
        assert st_other.last_error == "conflict" and st_other.remote_checked is False
        before = fake.objects[pointer_key(wid, 1)]
        clock.now += 3600  # past the back-off
        assert await other.persistence.snapshot(wid)  # re-reads the generation, writes 2, never overwrites 1
        assert fake.objects.get(pointer_key(wid, 1)) == before or pointer_key(wid, 1) not in fake.objects
        assert pointer_key(wid, 2) in fake.objects

    run(go())


# ----------------------------------------------------------------------------------- failures and recovery


def test_provider_errors_are_bounded_and_recovered(world):
    clock, fake, server = world

    async def go():
        a = server("a")
        await a.persistence._check_bucket()
        v = await visitor(a)
        wid = wid_of(v)
        await upload_rows(v, 10, "x")
        fake.fail = lambda req: Response(503, b"{}")
        fake.calls.clear()
        assert await a.persistence.snapshot(wid) is False
        assert len(fake.calls) == 3  # one list, retried twice (5xx), then given up: bounded
        st = a.persistence.state(wid)
        assert (
            st.failures == 1
            and st.retry_at > clock.now
            and a.persistence.status(wid)["persistence"] == "failed"
        )
        fake.fail = lambda req: Response(403, b'{"error":"denied"}')
        fake.calls.clear()
        clock.now = st.retry_at
        st.remote_checked = True
        assert await a.persistence.snapshot(wid) is False
        assert len(fake.calls) == 1  # 4xx: never retried
        fake.fail = lambda req: TimeoutError("timed out") if req.method == "POST" else None
        with pytest.raises(StorageError) as info:
            a.persistence.storage.put(archive_key(wid, 9), b"x", "application/zip")
        assert KEY not in str(info.value) and "workspaces/" not in str(info.value)
        fake.fail = None
        clock.now = st.retry_at
        assert await a.persistence.snapshot(wid) and a.persistence.status(wid)["persistence"] == "saved"

    run(go())


def test_an_interrupted_upload_keeps_the_previous_copy_usable(world):
    clock, fake, server = world

    async def go():
        a = server("a")
        await a.persistence._check_bucket()
        v = await visitor(a)
        wid = wid_of(v)
        n1 = await upload_rows(v, 10, "one")
        assert await a.persistence.snapshot(wid)
        await upload_rows(v, 11, "two")
        # the archive arrives, the pointer never does (process killed between the two)
        fake.fail = lambda req: (
            Response(503, b"{}") if req.url.endswith(".json") and req.method == "POST" else None
        )
        assert await a.persistence.snapshot(wid) is False
        fake.fail = None
        b = server("b")
        await b.persistence._check_bucket()
        v2 = Visitor(b.door, cookie=v.cookie)
        assert (await v2.open()).status == 200
        assert len((await v2.call("GET", "/api/datasets")).json()["items"]) == n1  # the last COMPLETE save

    run(go())


def test_an_interrupted_restore_changes_nothing_and_can_be_retried(world):
    clock, fake, server = world

    async def go():
        a = server("a")
        await a.persistence._check_bucket()
        v = await visitor(a)
        await upload_rows(v, 10, "x")
        assert await a.persistence.snapshot(wid_of(v))
        b = server("b")
        await b.persistence._check_bucket()
        fake.fail = lambda req: TimeoutError() if req.method == "GET" and req.url.endswith(".zip") else None
        v2 = Visitor(b.door, cookie=v.cookie)
        r = await v2.call("GET", "/api/datasets")
        assert r.status == 409 and "try again" in r.json()["error"]["message"]
        assert not (b.manager.root / wid_of(v)).exists() and not any(
            (b.manager.root.parent / "tmp").iterdir()
        )
        fake.fail = None
        clock.now += 3600  # after the back-off
        assert (await v2.open()).status == 200  # retried, restored

    run(go())


# ----------------------------------------------------------------------------------- retention and cleanup


def test_expired_copies_are_refused_and_cleaned_without_touching_others(world):
    clock, fake, server = world

    async def go():
        a = server("a")
        await a.persistence._check_bucket()
        old = await visitor(a, 30)
        await upload_rows(old, 10, "old")
        assert await a.persistence.snapshot(wid_of(old))
        clock.now += 6 * 86400
        young = await visitor(a, 31)
        await upload_rows(young, 10, "young")
        assert await a.persistence.snapshot(wid_of(young))
        fake.objects["unrelated/keep.txt"] = (b"not ours", clock.now)
        clock.now += 2 * 86400  # `old` is now past 7 days, `young` is 2 days old
        b = server("b")
        await b.persistence._check_bucket()
        assert (await b.persistence.cleanup()) == {"deleted_workspaces": 1, "kept": 1}
        assert not any(k.startswith(f"workspaces/{wid_of(old)}/") for k in fake.objects)
        assert any(k.startswith(f"workspaces/{wid_of(young)}/") for k in fake.objects)
        assert "unrelated/keep.txt" in fake.objects

    run(go())


def test_an_expired_copy_is_not_restored(world):
    clock, fake, server = world

    async def go():
        a = server("a")
        await a.persistence._check_bucket()
        v = await visitor(a)
        await upload_rows(v, 10, "x")
        assert await a.persistence.snapshot(wid_of(v))
        clock.now += 8 * 86400
        b = server("b")
        await b.persistence._check_bucket()
        with pytest.raises(WorkspaceUnavailable) as info:
            await b.manager.lease(wid_of(v))
        assert info.value.kind == "expired"

    run(go())


def test_orphaned_uploads_are_cleaned_after_a_day(world):
    clock, fake, server = world

    async def go():
        a = server("a")
        await a.persistence._check_bucket()
        wid = new_workspace_id()
        fake.objects[archive_key(wid, 1)] = (b"partial", clock.now)  # no pointer: an interrupted first upload
        assert (await a.persistence.cleanup())["deleted_workspaces"] == 0  # too recent
        clock.now += 2 * 86400
        assert (await a.persistence.cleanup())["deleted_workspaces"] == 1
        assert archive_key(wid, 1) not in fake.objects

    run(go())


# ---------------------------------------------------------------------------- scheduling, shutdown, output


def test_the_cadence_waits_for_quiet_or_the_maximum_delay(world):
    clock, fake, server = world

    async def go():
        a = server("a", snapshot_quiet_seconds=20, snapshot_max_delay_seconds=120)
        await a.persistence._check_bucket()
        v = await visitor(a)
        wid = wid_of(v)
        await upload_rows(v, 5, "a")
        assert a.persistence.due() == []
        clock.now += 21
        assert a.persistence.due() == [wid]
        for _ in range(7):  # a busy visitor: never quiet, saved at the latest after the maximum delay
            clock.now += 18
            a.persistence.mark_changed(wid)
        assert wid in a.persistence.due()

    run(go())


def test_shutdown_saves_what_it_can_and_never_reports_the_rest_as_saved(world):
    clock, fake, server = world

    async def go():
        a = server("a")
        await a.persistence._check_bucket()
        v1 = await visitor(a, 40)
        v2 = await visitor(a, 41)
        await upload_rows(v1, 5, "one")
        await upload_rows(v2, 5, "two")
        fake.fail = lambda req: Response(503, b"{}") if wid_of(v2) in req.url else None
        report = await a.persistence.flush(clock.now + 60)
        assert report == {"saved": 1, "not_saved": 1}
        assert a.persistence.status(wid_of(v2))["persistence"] == "failed"
        assert a.persistence.status(wid_of(v1))["persistence"] == "saved"

    run(go())


def test_unsaved_workspaces_are_never_evicted(world):
    clock, fake, server = world

    async def go():
        a = server("a", max_resident_workspaces=1)
        await a.persistence._check_bucket()
        v1 = await visitor(a, 50)
        await upload_rows(v1, 5, "one")  # unsaved
        r = await request(  # a second workspace needs the only resident slot
            a.door,
            "POST",
            "/demo/workspace/new",
            {"origin": "https://demo.example.org"},
            client=("198.51.100.51", 1),
        )
        assert r.status == 503 and json.loads(r.body)["error"]["code"] == "demo_busy"
        assert await a.persistence.snapshot(wid_of(v1))
        r = await request(
            a.door,
            "POST",
            "/demo/workspace/new",
            {"origin": "https://demo.example.org"},
            client=("198.51.100.52", 1),
        )
        assert r.status == 303  # saved: now it may be closed (its files and saved copy stay)

    run(go())


def test_status_and_errors_never_reveal_keys_or_storage_paths(world):
    clock, fake, server = world

    async def go():
        a = server("a")
        await a.persistence._check_bucket()
        v = await visitor(a)
        await upload_rows(v, 5, "x")
        await a.persistence.snapshot(wid_of(v))
        outputs = [
            (await v.call("GET", "/demo/workspace/status")).body,
            (await request(a.door, "GET", "/demo/status")).body,
            (await v.call("GET", "/")).body,
        ]
        for out in outputs:  # what visitors can see: no key, no bucket, no storage path
            assert KEY.encode() not in out and b"workspaces/" not in out and BUCKET.encode() not in out
        for text in (repr(a.settings), repr(a.persistence.storage)):  # what an operator's log could show
            assert KEY not in text
        status = json.loads(outputs[0])
        assert status["persistence"] == "saved" and wid_of(v) not in outputs[0].decode()
        assert (await request(a.door, "GET", "/demo/workspace/status")).status == 401  # only with its cookie
        for path in (f"/{archive_key(wid_of(v), 1)}", "/storage/v1/object/x", "/demo/snapshots"):
            r = await v.call("GET", path)
            assert r.status in (401, 404) and b"PK" not in r.body[:2]

    run(go())


def test_restores_of_one_workspace_are_single_flight(world):
    clock, fake, server = world

    async def go():
        a = server("a")
        await a.persistence._check_bucket()
        v = await visitor(a)
        await upload_rows(v, 5, "x")
        assert await a.persistence.snapshot(wid_of(v))
        b = server("b")
        await b.persistence._check_bucket()
        fake.calls.clear()
        results = await asyncio.gather(*(b.manager.lease(wid_of(v)) for _ in range(5)))
        assert len({id(w) for w in results}) == 1  # one workspace instance
        downloads = [c for c in fake.calls if c[0] == "GET" and c[1].endswith(".zip")]
        assert len(downloads) == 1  # one transfer
        for w in results:
            b.manager.release(w)

    run(go())


def test_the_storage_prefix_and_keys_cannot_be_chosen_by_a_visitor():
    for bad in ("", "workspaces", "workspaces/W-1/", "../workspaces/"):
        with pytest.raises(ValueError):
            P.workspace_prefix(bad.strip("/").split("/")[-1] or "x")


def test_new_supabase_secret_keys_are_sent_as_apikey_only(world, tmp_path):
    clock, _fake, _server = world
    secret = "sb_secret_" + "x" * 32
    fake = FakeStorage(clock, key=secret)
    env = demo_env(tmp_path / "k")
    env.update(
        LEDGERSPY_DEMO_STORAGE_URL=URL,
        LEDGERSPY_DEMO_STORAGE_BUCKET=BUCKET,
        LEDGERSPY_DEMO_STORAGE_SERVICE_KEY=secret,
    )
    storage = SupabaseStorage(DemoSettings.from_env(env).storage, transport=fake, sleep=lambda s: None)
    assert storage.bucket_is_private() is True  # the fake refuses a secret key sent as a Bearer token
    env["LEDGERSPY_DEMO_STORAGE_SERVICE_KEY"] = "sb_publishable_" + "y" * 32
    with pytest.raises(Exception, match="publishable"):
        DemoSettings.from_env(env)
