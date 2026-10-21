"""Best-effort persistence of visitor workspaces as snapshots in a private Supabase Storage bucket (Stage 3).

Nothing here claims durability it does not have: a change is "saved" only once the snapshot holding it and its
pointer have both been stored; until then the visitor is told it is not saved.

Lifecycle
1. **Creation**: a new workspace is a local copy of the seed; it is "unsaved" until its first snapshot.
2. **Writes**: every state-changing request that ran marks the workspace changed (a change counter, so a write
   during an upload is never mistaken for saved).
3. **Scheduling**: a single background task snapshots changed workspaces once they have been quiet for
   `snapshot_quiet_seconds`, or at the latest `snapshot_max_delay_seconds` after the first unsaved change; one
   snapshot at a time service-wide; failures back off (30 s, doubling, at most 10 min). The set of pending
   workspaces is bounded by the active-workspace cap.
4. **Snapshot**: under the workspace's persistence lock (serialises its snapshots and restores): the
   workspace's
   request lock is held only while the local, consistent copy is made (`snapshots.build_snapshot`), never
   during the upload. The archive goes to a new, never-reused key (create-only), then the pointer
   `manifest-<generation>.json` (create-only). Generations only increase, and restore always uses the highest,
   so an older upload can never replace a newer one. Two older generations are deleted afterwards.
5. **Restore** (first access after a restart or when the local copy is gone): single-flight per workspace,
   outside the manager's guard. The newest pointer is validated (format, workspace, sizes, retention), the
   archive downloaded with a size cap, checked against the pointer's SHA-256, extracted with full validation
   into a fresh staging folder, verified (audit chain, triggers, evidence) and only then renamed into place.
   A damaged or incompatible copy marks the workspace unavailable (it is never replaced by an empty one); a
   storage outage is reported as temporary and retried later.
6. **Shutdown**: after running requests end, changed workspaces are snapshotted until the grace period ends;
   the rest are reported as not saved.
7. **Expiry / cleanup**: hourly and at start-up, storage folders of expired workspaces (by the server-written
   `workspace_created_at` in the pointer, or by Supabase's own object timestamps) and orphaned objects older
   than a day are deleted, each folder only by its own validated keys.
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ledgerspy_demo.identity import WORKSPACE_ID
from ledgerspy_demo.seed import verify_workspace
from ledgerspy_demo.snapshots import FORMAT, VERSION, SnapshotError, build_snapshot, extract_snapshot
from ledgerspy_demo.storage import StorageError, SupabaseStorage, workspace_prefix

MANIFEST_NAME = re.compile(r"^manifest-([0-9]{12})\.json$")
SNAPSHOT_NAME = re.compile(r"^snapshot-([0-9]{12})\.zip$")
POINTER_MAX_BYTES = 16 * 1024
ORPHAN_SECONDS = 86400
CLEANUP_EVERY_SECONDS = 3600


def archive_key(wid: str, gen: int) -> str:
    return f"{workspace_prefix(wid)}snapshot-{gen:012d}.zip"


def pointer_key(wid: str, gen: int) -> str:
    return f"{workspace_prefix(wid)}manifest-{gen:012d}.json"


class RestoreUnavailable(Exception):
    """Storage could not be reached now; nothing changed; retry later."""


class NothingSaved(Exception):
    """No saved copy exists for this workspace."""


class RestoreExpired(Exception):
    """The saved copy is past retention."""


class RestoreInvalid(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class WorkspaceSave:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    changes: int = 0  # state-changing requests that ran
    saved_changes: int = 0  # `changes` covered by the newest stored snapshot
    first_unsaved: float | None = None
    last_change: float | None = None
    generation: int = 0  # newest committed generation (0: none known)
    remote_checked: bool = False
    last_saved_at: float | None = None
    failures: int = 0
    retry_at: float = 0.0
    last_error: str | None = None

    @property
    def dirty(self) -> bool:
        return self.changes > self.saved_changes


def _iso_time(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Persistence:
    def __init__(self, manager, storage: SupabaseStorage | None, temp_root: Path | None, clock=None) -> None:
        self.manager = manager
        self.storage = storage
        self.limits = manager.limits
        self.clock = clock or manager.clock
        self.temp_root = Path(temp_root) if temp_root is not None else Path(tempfile.gettempdir())
        self.mode = (
            "disabled" if storage is None else "unchecked"
        )  # disabled | unchecked | ready | unavailable
        self.disabled_reason = "not configured" if storage is None else None
        self._states: dict[str, WorkspaceSave] = {}
        self._slot = asyncio.Semaphore(1)  # one snapshot / restore transfer at a time, service-wide
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._next_cleanup = 0.0
        self._next_check = 0.0

    # ------------------------------------------------------------------ state
    @property
    def enabled(self) -> bool:
        return self.storage is not None and self.mode in ("ready", "unchecked", "unavailable")

    def state(self, wid: str) -> WorkspaceSave:
        if not WORKSPACE_ID.fullmatch(wid):
            raise ValueError("not a workspace ID")
        return self._states.setdefault(wid, WorkspaceSave())

    def forget(self, wid: str) -> None:
        self._states.pop(wid, None)

    def mark_changed(self, wid: str) -> None:
        if not self.enabled:
            return
        st = self.state(wid)
        now = self.clock()
        st.changes += 1
        st.last_change = now
        if st.first_unsaved is None:
            st.first_unsaved = now

    def is_dirty(self, wid: str) -> bool:
        st = self._states.get(wid)
        return bool(st and st.dirty and self.enabled)

    def status(self, wid: str) -> dict:
        """What the visitor is told: never "saved" for changes that are not in a stored snapshot."""
        if self.storage is None or self.mode == "disabled":
            return {
                "persistence": "disabled",
                "unsaved_changes": None,
                "last_saved_at": None,
                "message": "This demo does not save workspaces: your work is lost when the service restarts.",
            }
        st = self._states.get(wid) or WorkspaceSave()
        saved = (
            None
            if st.last_saved_at is None
            else datetime.fromtimestamp(st.last_saved_at).astimezone().isoformat()
        )
        if self.mode == "unavailable":
            state, msg = "unavailable", "Saving is temporarily unavailable; recent changes are not saved."
        elif st.dirty and st.failures:
            state, msg = "failed", "Your latest changes could not be saved yet; retrying. They are not saved."
        elif st.dirty:
            state, msg = "pending", "Your latest changes are not saved yet."
        elif st.last_saved_at is None:
            state, msg = "pending", "This workspace has not been saved yet."
        else:
            state, msg = "saved", "All changes so far are saved (best effort; kept at most 7 days)."
        return {"persistence": state, "unsaved_changes": st.dirty, "last_saved_at": saved, "message": msg}

    # ------------------------------------------------------------------ start / stop
    async def start(self) -> None:
        if self.storage is None:
            return
        await self._check_bucket()
        self._task = asyncio.ensure_future(self._run())

    async def _check_bucket(self) -> None:
        self._next_check = self.clock() + 300
        try:
            private = await asyncio.to_thread(self.storage.bucket_is_private)
        except StorageError:
            self.mode = "unavailable"
            return
        if not private:  # never store visitor data in a public bucket: persistence off for good
            self.mode, self.disabled_reason = "disabled", "the configured bucket is public"
            return
        self.mode = "ready"

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):  # the loop never dies; each step reports its own failures
                await self._tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=5)

    def due(self) -> list[str]:
        now, lim = self.clock(), self.limits
        out = []
        for wid, st in self._states.items():
            if not st.dirty or now < st.retry_at:
                continue
            quiet = st.last_change is not None and now - st.last_change >= lim.snapshot_quiet_seconds
            overdue = (
                st.first_unsaved is not None and now - st.first_unsaved >= lim.snapshot_max_delay_seconds
            )
            if quiet or overdue:
                out.append(wid)
        return out

    async def _tick(self) -> None:
        now = self.clock()
        if self.mode == "unavailable" and now >= self._next_check:
            await self._check_bucket()
        if self.mode not in ("ready",):
            return
        for wid in self.due():
            if self._stop.is_set():
                return
            await self.snapshot(wid)
        if now >= self._next_cleanup:
            self._next_cleanup = now + CLEANUP_EVERY_SECONDS
            await self.cleanup()

    async def flush(self, deadline: float) -> dict:
        """At shutdown: snapshot every changed workspace until `deadline` (clock time). Nothing is reported as
        saved unless it was stored."""
        await self.stop()
        report = {"saved": 0, "not_saved": 0}
        for wid in [w for w, st in self._states.items() if st.dirty]:
            if self.clock() >= deadline or self.mode != "ready":
                report["not_saved"] += 1
                continue
            report["saved" if await self.snapshot(wid) else "not_saved"] += 1
        return report

    # ------------------------------------------------------------------ snapshot
    def _backoff(self, st: WorkspaceSave, kind: str) -> None:
        st.failures += 1
        st.last_error = kind
        st.retry_at = self.clock() + min(30 * 2 ** (st.failures - 1), 600)

    async def _remote_generation(self, wid: str) -> int:
        entries = await asyncio.to_thread(self.storage.list_objects, workspace_prefix(wid))
        gens = [int(m.group(1)) for e in entries if (m := MANIFEST_NAME.fullmatch(e["name"]))]
        return max(gens, default=0)

    async def snapshot(self, wid: str) -> bool:
        """Store a snapshot of the workspace if it has unsaved changes. True when its changes are saved."""
        if self.storage is None or self.mode == "disabled":
            return False
        st = self.state(wid)
        async with st.lock, self._slot:
            if not st.dirty:
                return True
            try:
                if not st.remote_checked:  # after a restart: never reuse a generation already stored
                    st.generation = max(st.generation, await self._remote_generation(wid))
                    st.remote_checked = True
            except StorageError as exc:
                self._backoff(st, exc.kind)
                return False
            try:
                ws = await self.manager.lease(wid)
            except Exception:  # noqa: BLE001 — gone or unusable: nothing to save
                self.forget(wid)
                return False
            staging = reservation = None
            try:
                try:
                    await asyncio.wait_for(ws.lock.acquire(), timeout=self.limits.lock_wait_seconds)
                except TimeoutError:
                    self._backoff(st, "busy")
                    return False
                try:
                    covered = (
                        st.changes
                    )  # every change up to here is in this copy (no request runs meanwhile)
                    size = self.manager.workspace_size(ws)
                    reservation = self.manager.reserve(2 * size + 1024 * 1024, "snapshot")
                    staging = Path(tempfile.mkdtemp(prefix="snap-", dir=self.temp_root))
                    gen = st.generation + 1
                    snap = await asyncio.to_thread(
                        build_snapshot,
                        ws.path,
                        wid,
                        gen,
                        self.clock(),
                        ws.created_at,
                        self.limits.max_workspace_bytes,
                        staging,
                    )
                finally:
                    ws.lock.release()  # the upload runs without holding the visitor's workspace
                data = snap.archive.read_bytes()
                await asyncio.to_thread(self.storage.put, archive_key(wid, gen), data, "application/zip")
                pointer = {
                    "format": FORMAT,
                    "version": VERSION,
                    "workspace_id": wid,
                    "generation": gen,
                    "archive": archive_key(wid, gen),
                    "archive_sha256": snap.sha256,
                    "archive_bytes": snap.size,
                    "created_at": snap.manifest["created_at"],
                    "workspace_created_at": ws.created_at,
                }
                await asyncio.to_thread(
                    self.storage.put, pointer_key(wid, gen), json.dumps(pointer).encode(), "application/json"
                )
            except (StorageError, SnapshotError) as exc:
                kind = exc.kind if isinstance(exc, StorageError) else "snapshot_invalid"
                if kind == "conflict":  # another writer used this generation: re-read before trying again
                    st.remote_checked = False
                self._backoff(st, kind)
                return False
            except Exception:  # noqa: BLE001 — e.g. the budget refused the reservation
                self._backoff(st, "local_error")
                return False
            finally:
                if reservation is not None:
                    reservation.release()
                if staging is not None:
                    shutil.rmtree(staging, ignore_errors=True)
                self.manager.release(ws)
            st.generation = gen
            st.saved_changes = covered
            st.last_saved_at = snap.manifest["created_at"]
            st.failures, st.last_error, st.retry_at = 0, None, 0.0
            if not st.dirty:
                st.first_unsaved = None
            await self._prune(wid, gen)
            return not st.dirty

    async def _prune(self, wid: str, newest: int) -> None:
        """Delete generations older than the previous one (best effort; only this workspace's keys)."""
        try:
            entries = await asyncio.to_thread(self.storage.list_objects, workspace_prefix(wid))
        except StorageError:
            return
        old = []
        for e in entries:
            m = MANIFEST_NAME.fullmatch(e["name"]) or SNAPSHOT_NAME.fullmatch(e["name"])
            if m and int(m.group(1)) < newest - 1:
                old.append(workspace_prefix(wid) + e["name"])
        if old:
            with contextlib.suppress(StorageError):
                await asyncio.to_thread(self.storage.delete, old)

    # ------------------------------------------------------------------ restore
    def _valid_pointer(self, raw: bytes, wid: str, gen: int) -> dict:
        try:
            p = json.loads(raw)
        except ValueError as exc:
            raise RestoreInvalid("the saved copy's pointer is damaged") from exc
        ok = (
            isinstance(p, dict)
            and p.get("format") == FORMAT
            and p.get("version") == VERSION
            and p.get("workspace_id") == wid
            and p.get("generation") == gen
            and p.get("archive") == archive_key(wid, gen)
            and isinstance(p.get("archive_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", p["archive_sha256"]) is not None
            and isinstance(p.get("archive_bytes"), int)
            and 0 < p["archive_bytes"] <= self.limits.max_workspace_bytes + 1024 * 1024
            and isinstance(p.get("created_at"), (int, float))
            and isinstance(p.get("workspace_created_at"), (int, float))
        )
        if not ok:
            raise RestoreInvalid("the saved copy's pointer is invalid")
        return p

    async def restore(self, wid: str) -> None:
        """Bring back the newest saved copy of `wid` into the workspaces folder (not opened). Raises
        NothingSaved, RestoreExpired, RestoreInvalid or RestoreUnavailable; never touches an existing
        folder."""
        st = self.state(wid)
        if self.clock() < st.retry_at:
            raise RestoreUnavailable()
        async with st.lock, self._slot:
            target = self.manager.root / wid
            if target.exists():
                return  # restored meanwhile by another request
            staging = dest = reservation = None
            try:
                entries = await asyncio.to_thread(self.storage.list_objects, workspace_prefix(wid))
                gens = sorted(int(m.group(1)) for e in entries if (m := MANIFEST_NAME.fullmatch(e["name"])))
                if not gens:
                    raise NothingSaved()
                gen = gens[-1]
                raw = await asyncio.to_thread(self.storage.get, pointer_key(wid, gen), POINTER_MAX_BYTES)
                pointer = self._valid_pointer(raw, wid, gen)
                if self.clock() - pointer["workspace_created_at"] > self.manager.retention_seconds:
                    raise RestoreExpired()
                size = pointer["archive_bytes"]
                reservation = self.manager.reserve(size + self.limits.max_workspace_bytes, "restore")
                staging = Path(tempfile.mkdtemp(prefix="restore-", dir=self.temp_root))
                data = await asyncio.to_thread(self.storage.get, pointer["archive"], size)
                archive = staging / "snapshot.zip"
                archive.write_bytes(data)
                del data
                dest = self.manager.root / f".{wid}.restoring-{os.urandom(4).hex()}"
                dest.mkdir()
                manifest = await asyncio.to_thread(
                    extract_snapshot,
                    archive,
                    pointer["archive_sha256"],
                    size,
                    wid,
                    self.limits.max_workspace_bytes,
                    dest,
                )
                if (
                    manifest["generation"] != gen
                    or manifest["workspace_created_at"] != pointer["workspace_created_at"]
                ):
                    raise RestoreInvalid("the saved copy does not match its pointer")
                reason = await asyncio.to_thread(verify_workspace, dest)
                if reason is not None:
                    raise RestoreInvalid(f"the saved copy does not verify ({reason})")
                meta = dest / "workspace.json"
                if not meta.is_file():
                    meta.write_text(
                        json.dumps({"created_at": pointer["workspace_created_at"], "uploads": 0}),
                        encoding="utf-8",
                    )
                if target.exists():
                    return
                os.replace(dest, target)  # atomic: the workspace appears complete or not at all
                dest = None
            except StorageError as exc:
                if exc.kind == "not_found":
                    raise NothingSaved() from None
                self._backoff(st, exc.kind)
                raise RestoreUnavailable() from None
            except SnapshotError as exc:
                raise RestoreInvalid(exc.reason) from None
            finally:
                if reservation is not None:
                    reservation.release()
                for folder in (staging, dest):
                    if folder is not None:
                        _remove_owned(folder)
            st.generation, st.remote_checked = gen, True
            st.saved_changes = st.changes
            st.last_saved_at = pointer["created_at"]
            st.failures, st.retry_at, st.first_unsaved = 0, 0.0, None

    # ------------------------------------------------------------------ cleanup
    async def cleanup(self) -> dict:
        """Delete expired and orphaned saved copies. Each folder is handled only through its own validated
        keys;
        workspaces still active on this server are never deleted."""
        report = {"deleted_workspaces": 0, "kept": 0}
        if self.storage is None or self.mode != "ready":
            return report
        try:
            top = await asyncio.to_thread(self.storage.list_objects, "workspaces/")
        except StorageError:
            return report
        now, retention = self.clock(), self.manager.retention_seconds
        for folder in top:
            wid = folder.get("name", "")
            if not WORKSPACE_ID.fullmatch(wid) or folder.get("id") is not None:
                continue  # only workspace folders
            try:
                entries = await asyncio.to_thread(self.storage.list_objects, workspace_prefix(wid))
                keys = [
                    workspace_prefix(wid) + e["name"]
                    for e in entries
                    if MANIFEST_NAME.fullmatch(e["name"]) or SNAPSHOT_NAME.fullmatch(e["name"])
                ]
                stamps = [t for e in entries if (t := _iso_time(e.get("created_at"))) is not None]
                oldest = min(stamps, default=None)
                gens = sorted(int(m.group(1)) for e in entries if (m := MANIFEST_NAME.fullmatch(e["name"])))
                created = None
                if gens:
                    raw = await asyncio.to_thread(
                        self.storage.get, pointer_key(wid, gens[-1]), POINTER_MAX_BYTES
                    )
                    with contextlib.suppress(RestoreInvalid):
                        created = self._valid_pointer(raw, wid, gens[-1])["workspace_created_at"]
                expired = (created is not None and now - created > retention) or (
                    oldest is not None and now - oldest > retention + ORPHAN_SECONDS
                )
                orphan = not gens and oldest is not None and now - oldest > ORPHAN_SECONDS
                if (expired or orphan) and keys:
                    await asyncio.to_thread(self.storage.delete, keys)
                    report["deleted_workspaces"] += 1
                    self.forget(wid)
                else:
                    report["kept"] += 1
            except StorageError:
                continue
        return report


def _remove_owned(folder: Path) -> None:
    """Remove a staging folder this process created (read-only files included)."""

    def onexc(func, path, _exc):
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
            func(path)

    if folder.exists() and not folder.is_symlink():
        shutil.rmtree(folder, onexc=onexc)
