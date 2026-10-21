"""Visitor workspaces: creation from the seed, verified opening, leases, per-workspace locks, a bounded set of
resident app instances, quotas, the service storage budget and expiry (docs/demo/public-demo.md §3).

Concurrency model (one process, one event loop):
- the registry guard (`_guard`) serialises creation, opening, eviction and expiry, so a workspace never has
  two resident app instances;
- a lease (`lease` / `release`) marks a workspace in use; only a workspace with no lease and a free lock is
  evicted or expired;
- every request to a workspace runs under that workspace's own `asyncio.Lock` (taken by the front door), and
  every limit that depends on the workspace's state (upload counts, quota) is checked under that lock;
- every piece of work that can write holds a reservation on the service-wide `StorageBudget` (budget.py)
  until it can no longer write.

Lock order (no cycle is possible):
- `_guard` and a workspace lock are never awaited while the other is held by the same task: creation and
  opening take `_guard` only; a request takes only its workspace's lock (the sweep merely inspects locks);
- the budget's lock is a leaf: taken briefly under either of them, never held across an `await`.
"""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ledgerspy import storage_limits
from ledgerspy.api.app import create_app
from ledgerspy.cache import forget_namespace, namespace_for_path
from ledgerspy.config import EffectiveConfig, load_config
from ledgerspy_demo.allowances import RENDER_LIMIT_ENFORCED
from ledgerspy_demo.budget import Reservation, StorageBudget
from ledgerspy_demo.identity import WORKSPACE_ID, new_workspace_id, workspace_dir
from ledgerspy_demo.seed import remove_tree, verify_workspace
from ledgerspy_demo.settings import DemoSettings

META = "workspace.json"


def workspace_token(key: bytes, wid: str) -> str:
    """The workspace app's API bearer token: HMAC(server key, purpose || workspace ID), domain-separated
    from the cookie signature."""
    mac = hmac.new(key, b"ledgerspy-demo-api-token-v1|" + wid.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


# An executable that never exists (inside this package): OCR discovery then reports OCR unavailable.
OCR_DISABLED = str(Path(__file__).resolve().parent / "ocr-disabled-on-this-platform" / "tesseract")

LOST_REASON = (
    "the workspace is no longer on this server (the service restarted, and this demo does not keep work "
    "across restarts)"
)


class DemoLimitError(Exception):
    """A limit refused the request: `status` is the HTTP status, `code` a stable error code."""

    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


class WorkspaceUnavailable(Exception):
    """A valid workspace that cannot be used: `kind` is `restore_failed` or `expired`."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind, self.reason = kind, reason


@dataclass
class PendingRequest:
    """A state-changing request that outlived the visitor's request (front-door timeout or cancellation)."""

    key: tuple[str, str, bytes]  # method, path, query string
    started_at: float
    done: bool = False
    status: int | None = None  # the inner app's final status (None: no response, e.g. an exception)
    finished_at: float | None = None


@dataclass
class Activity:
    """Per-workspace accounting kept by the manager: reopening the app instance never resets it."""

    requests: deque = field(default_factory=deque)  # timestamps, for requests_per_minute
    uploads: deque = field(default_factory=deque)  # timestamps of accepted uploads, for uploads_per_hour
    pending: PendingRequest | None = None
    # Idempotency-Key -> entry (bounded, expiring; see frontdoor.IdempotencyEntry); kept in memory only
    idempotency: OrderedDict = field(default_factory=OrderedDict)
    # set when a request wrote more than its enforced bound allowed (it should be impossible): further
    # state-changing requests are refused, reads still work
    storage_blocked: str | None = None


@dataclass
class Workspace:
    id: str
    path: Path
    app: Any
    token: str
    created_at: float
    activity: Activity = field(default_factory=Activity)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    leases: int = 0
    last_used: float = 0.0

    @property
    def db_path(self) -> Path:
        return self.path / "ledgerspy.db"

    @property
    def requests(self) -> deque:
        return self.activity.requests

    @property
    def uploads(self) -> deque:
        return self.activity.uploads


def tree_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def demo_config(lim, path: Path) -> EffectiveConfig:
    """A workspace app's configuration: the offline defaults with the demo's stricter limits."""
    return load_config(
        overrides={
            "workspace": {"path": str(path)},
            "ingest": {
                "max_file_bytes": lim.max_upload_bytes,
                "max_rows": lim.max_rows,
                "max_columns": lim.max_columns,
                "xlsx": {
                    "max_uncompressed_bytes": lim.xlsx_max_uncompressed_bytes,
                    "max_zip_entries": lim.xlsx_max_zip_entries,
                },
            },
            "pdf": {
                "max_pages": lim.max_pdf_pages,
                "worker_timeout_seconds": lim.pdf_worker_timeout_seconds,
                "render": {"max_pixels": lim.max_pdf_render_pixels},
                # no OCR where the PDF worker's file-size limit cannot be enforced (see allowances)
                **({} if RENDER_LIMIT_ENFORCED else {"ocr": {"executable_candidates": [OCR_DISABLED]}}),
            },
        }
    )


class WorkspaceManager:
    def __init__(self, settings: DemoSettings, seed_dir: Path, frontend_dir: Path, clock=time.time) -> None:
        self.settings = settings
        self.limits = settings.limits
        self.data_dir = Path(settings.data_dir)
        self.root = self.data_dir / "workspaces"
        self.root.mkdir(parents=True, exist_ok=True)
        self.seed_dir = Path(seed_dir)
        self.frontend_dir = Path(frontend_dir)
        self.clock = clock
        self._guard = asyncio.Lock()
        self._resident: OrderedDict[str, Workspace] = OrderedDict()
        self._failed: dict[str, str] = {}  # workspace ID -> reason (kept until the cookie expires)
        self._creations: dict[str, deque] = {}  # client key -> creation timestamps
        self._service_creations: deque = deque()  # all creations, for the service-wide rate
        self._activity: dict[str, Activity] = {}  # workspace ID -> accounting (survives eviction)
        self.overshoots = 0  # requests that wrote more than their bound (should stay 0)
        self.persistence = None  # set by the app when snapshots are configured (persistence.Persistence)
        # one workspace's worth of head-room: what reservations cannot bound (see budget.py)
        self.budget = StorageBudget(
            self.limits.storage_budget_bytes, self.limits.max_workspace_bytes, self.service_bytes
        )

    # ------------------------------------------------------------------ configuration
    def config_for(self, path: Path) -> EffectiveConfig:
        """The workspace app's configuration: the offline defaults with the demo's stricter limits."""
        return demo_config(self.limits, path)

    @property
    def retention_seconds(self) -> int:
        return self.limits.retention_days * 86400

    # ------------------------------------------------------------------ accounting
    def service_bytes(self) -> int:
        return tree_bytes(self.data_dir)

    def workspace_ids_on_disk(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and WORKSPACE_ID.fullmatch(p.name))

    def reserve(self, nbytes: int, purpose: str) -> Reservation:
        """Hold `nbytes` of the service budget for work that may write them (the caller must release it)."""
        res = self.budget.reserve(nbytes, purpose)
        if res is None:
            raise DemoLimitError(
                507, "demo_storage_full", "The demo's storage budget is used up; try later.", 3600
            )
        return res

    def workspace_room(self, ws: Workspace) -> int:
        """Bytes this workspace may still grow by: its quota minus its measured size (negative when over)."""
        return self.limits.max_workspace_bytes - tree_bytes(ws.path)

    # ------------------------------------------------------------------ lifecycle
    async def create(self, client_key: str) -> Workspace:
        """A new workspace copied from the seed, returned LEASED (the caller must `release` it)."""
        async with self._guard:
            await self._sweep_locked()
            now = self.clock()
            for key, old in list(self._creations.items()):  # bounded: forget clients idle for an hour
                while old and now - old[0] > 3600:
                    old.popleft()
                if not old:
                    del self._creations[key]
            stamps = self._creations.setdefault(client_key, deque())
            while stamps and now - stamps[0] > 3600:
                stamps.popleft()
            if len(stamps) >= self.limits.new_workspaces_per_hour_per_client:
                raise DemoLimitError(
                    429, "too_many_workspaces", "Too many new demo workspaces; try later.", 3600
                )
            while self._service_creations and now - self._service_creations[0] > 3600:
                self._service_creations.popleft()
            if len(self._service_creations) >= self.limits.new_workspaces_per_hour:
                raise DemoLimitError(
                    429,
                    "demo_creation_limit",
                    "The demo has created many workspaces recently; try later.",
                    600,
                )
            if len(self.workspace_ids_on_disk()) >= self.limits.max_active_workspaces:
                raise DemoLimitError(503, "demo_full", "The demo is at capacity; try again later.", 600)
            self._require_room_locked()
            seed_bytes = tree_bytes(self.seed_dir)
            if seed_bytes > self.limits.max_workspace_bytes:
                raise DemoLimitError(503, "seed_too_large", "The demo seed exceeds the workspace limit.")
            reservation = self.reserve(seed_bytes + 4096, "workspace creation")  # the copy and its metadata
            wid = new_workspace_id()
            path = workspace_dir(self.root, wid)
            tmp = self.root / f".{wid}.creating-{secrets.token_hex(4)}"
            try:
                await asyncio.to_thread(shutil.copytree, self.seed_dir, tmp)
                (tmp / META).write_text(json.dumps({"created_at": now, "uploads": 0}), encoding="utf-8")
                os.replace(tmp, path)
            except BaseException:
                await asyncio.to_thread(remove_tree, tmp)
                raise
            finally:
                reservation.release()  # the copy is now measured on disk (or was removed)
            stamps.append(now)
            self._service_creations.append(now)
            ws = self._build(wid, path, now)
            ws.leases = 1  # leased before eviction runs: a new workspace is never its own victim
            self._resident[wid] = ws
            self._evict_locked()
            return ws

    async def lease(self, wid: str) -> Workspace:
        """The resident workspace for a validated ID (opened and verified if needed); the caller must
        `release` it. Raises WorkspaceUnavailable (never returns a replacement).

        A workspace whose local folder is gone is first restored from its saved copy when persistence is on:
        outside the guard (a network transfer never holds it), single-flight per workspace."""
        for _ in range(2):
            async with self._guard:
                ws = self._resident.get(wid)
                restore = ws is None and self._needs_restore(wid)
                if ws is None and not restore:
                    ws = await self._open_locked(wid)
                if ws is not None:
                    ws.leases += 1
                    ws.last_used = self.clock()
                    self._resident.move_to_end(wid)
                    return ws
            await self._restore(wid)
        raise WorkspaceUnavailable("restore_failed", "the workspace could not be opened")

    def _needs_restore(self, wid: str) -> bool:
        p = self.persistence
        return (
            p is not None
            and p.enabled
            and wid not in self._failed
            and not workspace_dir(self.root, wid).exists()
        )

    async def _restore(self, wid: str) -> None:
        from ledgerspy_demo.persistence import (
            NothingSaved,
            RestoreExpired,
            RestoreInvalid,
            RestoreUnavailable,
        )

        try:
            await self.persistence.restore(wid)
        except NothingSaved:
            self._failed[wid] = LOST_REASON
            raise WorkspaceUnavailable("restore_failed", LOST_REASON) from None
        except RestoreExpired:
            raise WorkspaceUnavailable("expired", "this demo workspace has expired") from None
        except RestoreInvalid as exc:
            self._failed[wid] = f"its saved copy cannot be used: {exc.reason}"
            raise WorkspaceUnavailable("restore_failed", self._failed[wid]) from None
        except RestoreUnavailable:
            raise WorkspaceUnavailable(
                "restore_failed", "its saved copy cannot be fetched right now; try again in a minute"
            ) from None

    def workspace_size(self, ws: Workspace) -> int:
        return tree_bytes(ws.path)

    def release(self, ws: Workspace) -> None:
        ws.leases -= 1
        ws.last_used = self.clock()

    async def _open_locked(self, wid: str) -> Workspace:
        if wid in self._failed:
            raise WorkspaceUnavailable("restore_failed", self._failed[wid])
        path = workspace_dir(self.root, wid)
        meta = path / META
        if not path.is_dir() or not meta.is_file():
            self._failed[wid] = LOST_REASON
            raise WorkspaceUnavailable("restore_failed", LOST_REASON)
        try:
            created = float(json.loads(meta.read_text(encoding="utf-8"))["created_at"])
        except (OSError, ValueError, KeyError, TypeError):
            self._failed[wid] = "the workspace metadata is unreadable"
            raise WorkspaceUnavailable("restore_failed", self._failed[wid]) from None
        if self.clock() - created > self.retention_seconds:
            raise WorkspaceUnavailable("expired", "this demo workspace has expired")
        self._require_room_locked()
        reason = await asyncio.to_thread(verify_workspace, path)
        if reason is not None:
            self._failed[wid] = reason  # kept (files untouched) until expiry; never replaced silently
            raise WorkspaceUnavailable("restore_failed", reason)
        ws = self._build(wid, path, created)
        ws.leases = 1  # the caller's lease (see `lease`), taken before eviction runs
        self._resident[wid] = ws
        self._evict_locked()
        ws.leases = 0
        return ws

    def _build(self, wid: str, path: Path, created: float) -> Workspace:
        # Stable for the workspace's lifetime, so a page opened before an eviction keeps working after the
        # instance is rebuilt; unguessable without the server key, and served only to the cookie holder.
        token = workspace_token(self.settings.session_key, wid)
        app = create_app(self.config_for(path), path / "ledgerspy.db", token, frontend_dir=self.frontend_dir)
        app.state.actor = f"demo:{wid[2:10]}"  # pseudonymous; never an OS user, IP or name
        activity = self._activity.setdefault(wid, Activity())
        return Workspace(
            id=wid,
            path=path,
            app=app,
            token=token,
            created_at=created,
            activity=activity,
            last_used=self.clock(),
        )

    def _require_room_locked(self) -> None:
        """Refuse (rather than exceed the resident bound) when every resident instance is in use."""
        if len(self._resident) >= self.limits.max_resident_workspaces and not any(
            self._idle(w) for w in self._resident.values()
        ):
            raise DemoLimitError(503, "demo_busy", "The demo is busy; try again shortly.", 30)

    def _idle(self, ws: Workspace) -> bool:
        """Not in use, and (with persistence) nothing unsaved: such a workspace is never closed."""
        unsaved = self.persistence is not None and self.persistence.is_dirty(ws.id)
        return ws.leases == 0 and not ws.lock.locked() and not unsaved

    def _evict_locked(self) -> None:
        """Close idle app instances (least recently used first) until the resident bound holds."""
        while len(self._resident) > self.limits.max_resident_workspaces:
            victim = next((w for w in self._resident.values() if self._idle(w)), None)
            if victim is None:
                return
            self._close(victim)

    def _close(self, ws: Workspace) -> None:
        self._resident.pop(ws.id, None)
        storage_limits.clear(ws.path, ws.db_path)
        forget_namespace(namespace_for_path(ws.db_path))  # engine results computed from its database
        forget_namespace(namespace_for_path(ws.path))  # supervised inference results (keyed by folder)

    async def sweep(self) -> list[str]:
        async with self._guard:
            return await self._sweep_locked()

    async def _sweep_locked(self) -> list[str]:
        """Delete expired workspaces (never one in use) and abandoned temporary folders."""
        now = self.clock()
        removed = []
        for wid in self.workspace_ids_on_disk():
            path = workspace_dir(self.root, wid)
            try:
                created = float(json.loads((path / META).read_text(encoding="utf-8"))["created_at"])
            except (OSError, ValueError, KeyError, TypeError):
                created = path.stat().st_mtime  # unreadable metadata: expire by folder age
            if now - created <= self.retention_seconds:
                continue
            if self.persistence is not None:
                self.persistence.forget(wid)  # an expired workspace is not saved any more
            ws = self._resident.get(wid)
            if ws is not None:
                if not self._idle(ws):
                    continue
                self._close(ws)
            await asyncio.to_thread(remove_tree, path)
            self._failed.pop(wid, None)
            self._activity.pop(wid, None)
            removed.append(wid)
        for p in self.root.iterdir():
            if p.name.startswith(".W-") and now - p.stat().st_mtime > 3600:
                await asyncio.to_thread(remove_tree, p)
        return removed

    # ------------------------------------------------------------------ per-workspace accounting
    def record_upload(self, ws: Workspace) -> None:
        meta = ws.path / META
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["uploads"] = int(data.get("uploads", 0)) + 1
        tmp = meta.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, meta)

    def uploads_so_far(self, ws: Workspace) -> int:
        try:
            return int(json.loads((ws.path / META).read_text(encoding="utf-8")).get("uploads", 0))
        except (OSError, ValueError, TypeError):
            return self.limits.max_uploads_per_workspace  # unknown: refuse further uploads

    @property
    def resident_ids(self) -> list[str]:
        return list(self._resident)
