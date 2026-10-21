"""The public front door of the hosted demo (ASGI). See docs/demo/public-demo.md §2.

For every request it:
1. checks the public Host, Origin and Fetch-Metadata (a cross-site request is refused, except a top-level
   navigation to `/`, which only gets a same-origin bounce page);
2. reads the signed workspace cookie. Without one, a page view gets a static landing page and nothing is
   allocated; a workspace is created only by the explicit `POST /demo/workspace/new` (same-origin, rate- and
   capacity-limited). An API request without a valid cookie is refused, and a valid but unusable workspace is
   reported, never replaced;
3. applies the demo limits: the request rate and the request's own size first, then, under the workspace's
   lock, everything that depends on the workspace's state (upload counts, quota), the request's storage bound
   (`allowances.plan`: database cap, journal, files, PDF renders, each enforced) and a reservation of that
   bound on the service-wide storage budget; a state-changing request with no known bound is refused;
4. forwards the request, still under the lock, to that workspace's own, unchanged LedgerSpy app, presenting
   the app's loopback Host. It never adds an `Authorization` header: the workspace app's token check, body
   bound and CSP apply to every API call as offline.

State-changing requests may carry an `Idempotency-Key`: a retry with the same key, route and body gets the
recorded result instead of running again; the same key with anything different is refused (409). Requests that
run the PDF worker share a service-wide limit. During a graceful shutdown new requests are refused (503) and
running work is waited for, up to a deadline after which PDF worker process trees are killed.

The lock and the reservation are released exactly once (`_Job.finish`), when the work can no longer write.
Work that outlives the visitor's request (a processing timeout, or the request being cancelled) cannot be
stopped safely, because a worker thread may be writing: it keeps both until it really ends, its outcome is
then counted, and the visitor is told that it may still complete (never that it failed).
"""

import asyncio
import contextlib
import hashlib
import hmac
import html
import json
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import parse_qsl

from ledgerspy import storage_limits
from ledgerspy.api.security import SESSION_COOKIE, security_headers
from ledgerspy.config.schema import LOOPBACK_HOST
from ledgerspy_demo import allowances
from ledgerspy_demo.frontend import BANNER_TEXT, EPHEMERAL_NOTE, PERSISTENT_NOTE
from ledgerspy_demo.identity import read_cookie, sign_cookie
from ledgerspy_demo.settings import DemoSettings
from ledgerspy_demo.workspace import (
    DemoLimitError,
    PendingRequest,
    Workspace,
    WorkspaceManager,
    WorkspaceUnavailable,
    tree_bytes,
)

COOKIE = "ls_demo_ws"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
UPLOAD = re.compile(r"^/api/cases/[^/]+/files$")
PAGE_PATHS = frozenset({"/", "/index.html"})
FRONT_CSP = "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
DROPPED = frozenset(
    {
        b"host",
        b"origin",
        b"cookie",
        b"forwarded",
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-proto",
        b"x-real-ip",
    }
)

RECONCILE_SLACK_BYTES = 64 * 1024  # the upload counter's metadata rewrite and file-system rounding
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
IDEMPOTENCY_MAX_ENTRIES = 16  # per workspace
IDEMPOTENCY_MAX_RESPONSE = 64 * 1024  # a larger response is recorded by status only


@dataclass
class IdempotencyEntry:
    route: tuple  # method, path, sorted query
    expires: float
    body_sha256: str | None = None  # known once the body has been read completely
    done: bool = False
    status: int | None = None
    content_type: str = "application/json"
    response: bytes | None = None  # None: too large (or none) to replay


def _canonical_route(method: str, scope: dict) -> tuple:
    query = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
    return (method, scope["path"], tuple(sorted(query)))


def _storage_full(exc: BaseException | None) -> bool:
    """Whether an exception (or one it was raised from) is SQLite refusing to grow the database."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, sqlite3.OperationalError) and "full" in str(exc).lower():
            return True
        exc = exc.__cause__ or exc.__context__
    return False


Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]


class _Job:
    """One request's hold on its workspace: the workspace lock (already acquired), an optional budget
    reservation and the upload accounting. `finish` runs exactly once: directly when the request was refused,
    failed or answered in time, otherwise from the completion callback of the work that outlived it."""

    def __init__(self, door: "FrontDoor", ws: Workspace, upload: bool) -> None:
        self.door, self.ws, self.upload = door, ws, upload
        self.reservation = None
        self.plan: allowances.StoragePlan | None = None
        self.ws_before = 0
        self.db_before = 0
        self.status: int | None = None
        self.handed_off = False
        self.finished = False
        self.pdf_slot = False  # holds one of the service-wide PDF job slots
        self.forwarded = False  # the request reached the workspace app
        self.idempotency: IdempotencyEntry | None = None
        self.idempotency_key: str | None = None
        self.body_hash = hashlib.sha256()
        self.body_complete = False
        self.response = bytearray()
        self.response_type = "application/json"
        self.response_overflow = False
        door._job_started()

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        try:
            if self.plan is not None:
                self.door._reconcile(self)
                persistence = self.door.manager.persistence
                if self.forwarded and self.plan.op.writes_db and persistence is not None:
                    persistence.mark_changed(self.ws.id)
            if self.upload and self.status is not None and 200 <= self.status < 300:
                self.ws.uploads.append(self.door.clock())
                self.door.manager.record_upload(self.ws)
            self.door._finish_idempotency(self)
        finally:
            if self.plan is not None:
                storage_limits.clear(self.ws.path, self.ws.db_path)
            if self.reservation is not None:
                self.reservation.release()
            if self.pdf_slot:
                self.door.pdf_slots.release()
            self.ws.lock.release()
            self.door._job_ended()


def _headers(scope: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in scope.get("headers", []):
        name = k.decode("latin-1").lower()
        out[name] = v.decode("latin-1") if name not in out else out[name] + ", " + v.decode("latin-1")
    return out


class FrontDoor:
    def __init__(self, settings: DemoSettings, manager: WorkspaceManager, clock=time.time) -> None:
        self.settings = settings
        self.manager = manager
        self.limits = settings.limits
        self.clock = clock
        self.https = settings.public_origin.startswith("https://")
        self.inner_port = manager.config_for(manager.root).settings.server.port
        self.processing_timeout = float(self.limits.max_processing_seconds)
        self.lock_wait_seconds = float(self.limits.lock_wait_seconds)
        self.pdf_slots = asyncio.Semaphore(self.limits.max_concurrent_pdf_jobs)
        self.closing = False
        self.jobs = 0  # requests holding a workspace lock (including work that outlived its request)
        self._idle = asyncio.Event()
        self._idle.set()
        self.shutdown_report: dict | None = None

    def _job_started(self) -> None:
        self.jobs += 1
        self._idle.clear()

    def _job_ended(self) -> None:
        self.jobs -= 1
        if self.jobs == 0:
            self._idle.set()

    async def shutdown(self, grace: float | None = None, after_kill: float = 10.0) -> dict:
        """Graceful stop: refuse new requests, wait for running work up to `grace` seconds, then kill PDF
        worker process trees (the only work that can be stopped safely) and wait briefly for the rest.
        Reservations and locks are released only by the work itself, never here."""
        self.closing = True
        grace = float(self.limits.shutdown_grace_seconds) if grace is None else grace
        report = {"running_at_start": self.jobs, "killed_pdf_workers": 0, "still_running": 0}
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=grace)
        except TimeoutError:
            report["killed_pdf_workers"] = storage_limits.kill_all_workers()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._idle.wait(), timeout=after_kill)
        report["still_running"] = self.jobs
        self.shutdown_report = report
        return report

    # ------------------------------------------------------------------ helpers
    def client_key(self, scope: dict, headers: dict[str, str]) -> str:
        """The rate-limit key: the TCP peer, or with N trusted proxies the N-th address from the right of
        X-Forwarded-For. Anything further left is client-controlled and ignored. Kept only as an HMAC."""
        peer = (scope.get("client") or ("unknown", 0))[0]
        hops = self.settings.trusted_proxy_hops
        if hops:
            chain = [p.strip() for p in headers.get("x-forwarded-for", "").split(",") if p.strip()]
            if len(chain) >= hops:
                peer = chain[-hops]
        return hmac.new(
            self.settings.session_key, peer.encode("utf-8", "replace"), hashlib.sha256
        ).hexdigest()

    def _cookie_value(self, headers: dict[str, str]) -> str | None:
        raw = headers.get("cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # noqa: BLE001 — a malformed Cookie header is no cookie
            return None
        morsel = jar.get(COOKIE)
        return morsel.value if morsel is not None else None

    def _set_cookie(self, wid: str) -> bytes:
        value = sign_cookie(self.settings.session_key, wid, int(self.clock()))
        attrs = (
            f"{COOKIE}={value}; Max-Age={self.manager.retention_seconds}; Path=/; HttpOnly; SameSite=Strict"
        )
        return (attrs + "; Secure").encode("latin-1")  # always Secure (localhost is a secure context)

    def _base_headers(self, content_type: str) -> list[tuple[bytes, bytes]]:
        hs = [(k.lower().encode(), v.encode()) for k, v in security_headers(FRONT_CSP).items()]
        hs.append((b"content-type", content_type.encode()))
        if self.https:
            hs.append((b"strict-transport-security", b"max-age=31536000"))
        return hs

    async def _respond(
        self,
        send: Send,
        status: int,
        body: bytes,
        content_type: str = "application/json",
        extra: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        headers = self._base_headers(content_type) + (extra or [])
        headers.append((b"content-length", str(len(body)).encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _error(
        self,
        send: Send,
        status: int,
        code: str,
        message: str,
        retry_after: int | None = None,
        details: dict | None = None,
    ):
        error = {"code": code, "message": message, "details": details or {}}
        body = json.dumps({"status": "rejected", "error": error})
        extra = [(b"retry-after", str(retry_after).encode())] if retry_after else None
        await self._respond(send, status, body.encode(), extra=extra)

    async def _landing(self, send: Send) -> None:
        note = (PERSISTENT_NOTE if self.settings.persistent else EPHEMERAL_NOTE).format(
            days=self.limits.retention_days
        )
        body = (
            '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>LedgerSpy public demo</title>'
            "</head><body><h1>LedgerSpy public demo</h1>"
            f'<p role="alert"><strong>{html.escape(BANNER_TEXT)}</strong></p><p>{html.escape(note)}</p>'
            "<p>A demo workspace is a private copy of a synthetic company. It is created only when you ask "
            "for one.</p>"
            '<form method="post" action="/demo/workspace/new"><button type="submit">Start a demo workspace '
            "(synthetic data)</button></form></body></html>"
        )
        await self._respond(send, 200, body.encode("utf-8"), "text/html; charset=utf-8")

    async def _page(self, send: Send, status: int, title: str, text: str, new_button: bool, extra=None):
        form = (
            '<form method="post" action="/demo/workspace/new"><button type="submit">Start a new demo '
            "workspace</button></form>"
            if new_button
            else ""
        )
        body = (
            f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>{html.escape(title)}</title>'
            f"</head><body><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>{form}</body></html>"
        )
        await self._respond(send, status, body.encode("utf-8"), "text/html; charset=utf-8", extra)

    # ------------------------------------------------------------------ ASGI
    async def __call__(self, scope: dict, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await self.manager.sweep()
                    if self.manager.persistence is not None:
                        await self.manager.persistence.start()
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    started = self.clock()
                    await self.shutdown()
                    if self.manager.persistence is not None:  # unsaved work: best effort, same deadline
                        report = await self.manager.persistence.flush(
                            started + self.limits.shutdown_grace_seconds
                        )
                        self.shutdown_report = {**(self.shutdown_report or {}), "snapshots": report}
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return  # no websockets
        headers = _headers(scope)
        method, path = scope["method"], scope["path"]
        if "transfer-encoding" in headers:
            # Only Content-Length framing is accepted: a chunked body (alone, or beside a Content-Length that
            # some parsers then ignore) could disagree with the declared size every limit is checked against.
            return await self._error(
                send, 400, "transfer_encoding_not_supported", "send the body with a Content-Length instead"
            )
        if path == "/healthz" and method in ("GET", "HEAD"):
            # the platform's health check (it may not send the public Host): a fixed answer, no data at all
            if self.closing:
                return await self._error(send, 503, "shutting_down", "stopping", 30)
            return await self._respond(send, 200, b'{"status":"ok"}')
        if headers.get("host", "").lower() != self.settings.public_host:
            return await self._error(send, 400, "invalid_host", "invalid Host header")
        if self.closing:
            return await self._error(
                send, 503, "shutting_down", "the demo is restarting; try again shortly", 30
            )
        origin = headers.get("origin")
        if origin is not None and origin.lower() != self.settings.public_origin:
            return await self._error(send, 403, "cross_origin", "cross-origin request refused")
        site = headers.get("sec-fetch-site", "same-origin")
        if site not in ("same-origin", "none"):
            navigation = (
                method == "GET"
                and path == "/"
                and headers.get("sec-fetch-mode") == "navigate"
                and headers.get("sec-fetch-dest") == "document"
            )
            if not navigation:
                return await self._error(send, 403, "cross_site", "cross-site request refused")
            body = (
                b'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta http-equiv="refresh" '
                b'content="0; url=/"><title>LedgerSpy demo</title></head><body><a href="/">Open the demo</a>'
                b"</body></html>"
            )
            return await self._respond(send, 200, body, "text/html; charset=utf-8")
        if path.startswith("/demo/"):
            return await self._demo_route(scope, headers, send, method, path)
        return await self._workspace_request(scope, receive, send, headers, method, path)

    async def _demo_route(self, scope: dict, headers: dict[str, str], send: Send, method: str, path: str):
        if path == "/demo/workspace/new" and method == "POST":
            if headers.get("origin", "").lower() != self.settings.public_origin:  # Origin required for POST
                return await self._error(send, 403, "origin_required", "same-origin POST required")
            try:
                ws = await self.manager.create(self.client_key(scope, headers))
            except DemoLimitError as exc:
                return await self._error(send, exc.status, exc.code, exc.message, exc.retry_after)
            self.manager.release(ws)
            extra = [(b"set-cookie", self._set_cookie(ws.id)), (b"location", b"/")]
            return await self._respond(send, 303, b"", "text/plain", extra)
        if path == "/demo/workspace/status" and method == "GET":
            wid = read_cookie(
                self.settings.session_key,
                self._cookie_value(headers),
                int(self.clock()),
                self.manager.retention_seconds,
            )
            if wid is None:
                return await self._error(send, 401, "no_demo_workspace", "start a demo workspace first")
            persistence = self.manager.persistence
            if persistence is None:
                body = {
                    "persistence": "disabled",
                    "unsaved_changes": None,
                    "last_saved_at": None,
                    "message": "This demo does not save workspaces: work is lost when the service restarts.",
                }
            else:
                body = persistence.status(wid)
            return await self._respond(send, 200, json.dumps(body).encode())
        if path == "/demo/status" and method == "GET":
            body = {
                "mode": "public-demo",
                "synthetic_data_only": True,
                "persistent": self.settings.persistent,
                "persistence": (
                    "disabled" if self.manager.persistence is None else self.manager.persistence.mode
                ),
                "retention_days": self.limits.retention_days,
                "limits": {
                    "max_upload_bytes": self.limits.max_upload_bytes,
                    "max_rows": self.limits.max_rows,
                    "max_pdf_pages": self.limits.max_pdf_pages,
                    "max_workspace_bytes": self.limits.max_workspace_bytes,
                },
            }
            return await self._respond(send, 200, json.dumps(body).encode())
        return await self._error(send, 404, "not_found", "not found")

    async def _workspace_request(self, scope, receive, send, headers, method, path):
        now = int(self.clock())
        cookie = self._cookie_value(headers)
        wid = read_cookie(self.settings.session_key, cookie, now, self.manager.retention_seconds)
        page = method in ("GET", "HEAD") and path in PAGE_PATHS
        new_cookie: bytes | None = None
        if wid is None:
            if page:  # a static landing page: nothing is allocated for an anonymous visit
                return await self._landing(send)
            return await self._error(send, 401, "no_demo_workspace", "start a demo workspace first")
        else:
            try:
                ws = await self.manager.lease(wid)
            except WorkspaceUnavailable as exc:
                title = "Demo workspace expired" if exc.kind == "expired" else "Demo workspace unavailable"
                if page:
                    return await self._page(
                        send, 409, title, f"Your demo workspace cannot be used: {exc.reason}.", True
                    )
                return await self._error(send, 409, f"workspace_{exc.kind}", exc.reason)
            except DemoLimitError as exc:
                return await self._error(send, exc.status, exc.code, exc.message, exc.retry_after)
        try:
            await self._serve(ws, scope, receive, send, headers, method, path, new_cookie)
        finally:
            self.manager.release(ws)

    async def _serve(self, ws: Workspace, scope, receive, send, headers, method, path, new_cookie):
        lim = self.limits
        now = self.clock()
        while ws.requests and now - ws.requests[0] > 60:
            ws.requests.popleft()
        if len(ws.requests) >= lim.requests_per_minute:
            return await self._error(send, 429, "rate_limited", "too many requests; slow down", 60)
        ws.requests.append(now)
        upload = method == "POST" and UPLOAD.match(path) is not None
        declared = headers.get("content-length", "")
        op = allowances.classify(method, path)
        if op is None:  # a state-changing request whose disk growth is not bounded: never admitted
            return await self._error(
                send, 403, "operation_not_available", "This operation is not available in the public demo."
            )
        if upload:  # checks of the request alone (no workspace state), before waiting for the lock
            if not declared.isdigit():
                return await self._error(send, 411, "length_required", "uploads need a Content-Length")
            if int(declared) > lim.max_upload_bytes:
                return await self._error(
                    send, 413, "file_too_large", f"demo uploads are limited to {lim.max_upload_bytes} bytes"
                )
        key = headers.get("idempotency-key") if method in MUTATING else None
        if key is not None and not IDEMPOTENCY_KEY.fullmatch(key):
            return await self._error(
                send, 400, "invalid_idempotency_key", "an Idempotency-Key is 8-64 letters, digits, '-' or '_'"
            )
        route = _canonical_route(method, scope)
        if key is not None and (entry := self._idempotency_entry(ws, key)) is not None and not entry.done:
            return await self._error(
                send,
                409,
                "idempotent_request_in_progress",
                "the request with this Idempotency-Key is still running",
            )
        if (refusal := self._pending_refusal(ws, method, scope)) is not None:
            return await self._error(send, *refusal)
        try:
            await asyncio.wait_for(ws.lock.acquire(), timeout=self.lock_wait_seconds)
        except TimeoutError:
            return await self._error(send, 503, "workspace_busy", "this workspace is busy; retry shortly", 5)
        job = _Job(self, ws, upload)
        try:
            if key is not None and (entry := self._idempotency_entry(ws, key)) is not None:
                return await self._replay(job, entry, route, receive, send, declared)
            # rechecked under the lock: the workspace's state may have changed while this request waited
            if (refusal := self._pending_refusal(ws, method, scope)) is not None:
                return await self._error(send, *refusal)
            if not allowances.RENDER_LIMIT_ENFORCED and op is allowances.PDF_PAGE_IMAGE:
                raise DemoLimitError(
                    403,
                    "not_available_on_this_platform",
                    "PDF page images are not rendered on this server (no enforceable file-size limit).",
                )
            self._admit(job, op, method, declared, scope)
            if job.plan is not None and job.plan.render:  # runs the PDF worker: one of the service-wide slots
                try:
                    await asyncio.wait_for(self.pdf_slots.acquire(), timeout=self.lock_wait_seconds)
                except TimeoutError:
                    raise DemoLimitError(
                        503, "pdf_busy", "the demo is processing another PDF; try again shortly", 10
                    ) from None
                job.pdf_slot = True
            if key is not None:
                job.idempotency = self._register_idempotency(ws, key, route)
                job.idempotency_key = key
            await self._forward(job, scope, receive, send, method, path, new_cookie, headers)
        except DemoLimitError as exc:
            return await self._error(send, exc.status, exc.code, exc.message, exc.retry_after)
        finally:
            if not job.handed_off:
                job.finish()

    # ------------------------------------------------------------------ idempotency
    def _idempotency_entry(self, ws: Workspace, key: str) -> IdempotencyEntry | None:
        reg = ws.activity.idempotency
        now = self.clock()
        for k in [k for k, e in reg.items() if e.done and e.expires <= now]:
            del reg[k]
        return reg.get(key)

    def _register_idempotency(self, ws: Workspace, key: str, route: tuple) -> IdempotencyEntry:
        reg = ws.activity.idempotency
        while len(reg) >= IDEMPOTENCY_MAX_ENTRIES:
            oldest = next((k for k, e in reg.items() if e.done), None)
            if oldest is None:  # every entry still running (cannot happen: one write runs per workspace)
                raise DemoLimitError(429, "idempotency_registry_full", "too many requests in flight", 10)
            del reg[oldest]
        entry = IdempotencyEntry(route=route, expires=self.clock() + self.limits.idempotency_ttl_seconds)
        reg[key] = entry
        return entry

    def _finish_idempotency(self, job: _Job) -> None:
        entry = job.idempotency
        if entry is None:
            return
        if not job.body_complete:  # the body was not read in full: no reliable fingerprint, no replay
            job.ws.activity.idempotency.pop(job.idempotency_key, None)
            return
        entry.body_sha256 = job.body_hash.hexdigest()
        entry.done, entry.status = True, job.status
        entry.content_type = job.response_type
        entry.response = None if job.response_overflow else bytes(job.response)
        entry.expires = self.clock() + self.limits.idempotency_ttl_seconds

    async def _replay(self, job: _Job, entry: IdempotencyEntry, route: tuple, receive, send, declared: str):
        """A request whose Idempotency-Key is known: compare route and body, then replay or refuse."""
        reused = (409, "idempotency_key_reused", "this Idempotency-Key was used for a different request")
        if entry.route != route:
            return await self._error(send, *reused)
        digest, size, limit = hashlib.sha256(), 0, int(declared) if declared.isdigit() else 0
        while True:  # the body is hashed as it streams and discarded: never buffered
            message = await receive()
            if message["type"] != "http.request":
                return await self._error(send, 400, "incomplete_body", "the request body was not received")
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                return await self._error(
                    send, 400, "body_exceeds_content_length", "body longer than declared"
                )
            digest.update(chunk)
            if not message.get("more_body", False):
                break
        if digest.hexdigest() != entry.body_sha256:
            return await self._error(send, *reused)
        if entry.response is None:
            return await self._error(
                send,
                409,
                "idempotent_result_unavailable",
                "this request already ran; its answer was too large to keep. Reload the page to see the "
                "result.",
                details={"previous_status": entry.status},
            )
        await self._respond(
            send, entry.status or 500, entry.response, entry.content_type, [(b"idempotent-replayed", b"true")]
        )

    def _admit(
        self, job: _Job, op: allowances.OperationClass, method: str, declared: str, scope: dict
    ) -> None:
        """The workspace-state limits and the storage bound of a request; called under the workspace's lock,
        so no concurrent request of the same workspace can pass them on a stale count or size."""
        ws, lim = job.ws, self.limits
        if method in MUTATING and ws.activity.storage_blocked:
            raise DemoLimitError(507, "workspace_storage_overshoot", ws.activity.storage_blocked)
        if job.upload:
            now = self.clock()
            while ws.uploads and now - ws.uploads[0] > 3600:
                ws.uploads.popleft()
            if len(ws.uploads) >= lim.uploads_per_hour:
                raise DemoLimitError(429, "upload_rate_limited", "too many uploads; try later", 3600)
            if self.manager.uploads_so_far(ws) >= lim.max_uploads_per_workspace:
                raise DemoLimitError(
                    413, "upload_limit", f"a demo workspace accepts {lim.max_uploads_per_workspace} uploads"
                )
        # measured now: earlier writes count at their real size
        job.ws_before = tree_bytes(ws.path)
        job.db_before = ws.db_path.stat().st_size
        query = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1")))
        try:
            plan = allowances.plan(
                op,
                lim,
                ws_bytes=job.ws_before,
                db_bytes=job.db_before,
                page_size=allowances.sqlite_page_size(ws.db_path),
                declared_bytes=int(declared) if declared.isdigit() else 0,
                query=query,
            )
        except allowances.InsufficientRoom:
            raise DemoLimitError(
                413,
                "workspace_quota_exceeded",
                f"This demo workspace is limited to {lim.max_workspace_bytes} bytes.",
            ) from None
        if plan.total:
            job.reservation = self.manager.reserve(plan.total, f"{op.name} in {ws.id}")
        # enforced from here: the database cannot grow past its cap (reads cannot write at all), and file
        # output beyond the plan is refused before it is written
        storage_limits.set_db_max_bytes(ws.db_path, plan.db_cap, read_only=not op.writes_db)
        storage_limits.set_output_allowance(ws.path, plan.files if op.files in ("report", "inference") else 0)
        job.plan = plan

    def _reconcile(self, job: _Job) -> None:
        """After the work has stopped: compare the workspace's real growth with its enforced bound. Exceeding
        it should be impossible; if it happens, further state-changing requests are refused (fail closed)."""
        ws, plan = job.ws, job.plan
        after = tree_bytes(ws.path)
        db_after = ws.db_path.stat().st_size if ws.db_path.exists() else 0
        db_grown = db_after - job.db_before
        files_grown = (after - db_after) - (job.ws_before - job.db_before)  # everything but the database
        # checked separately, so excess file output can never hide inside the database's allowance
        if db_grown > plan.db_growth or files_grown > plan.files + RECONCILE_SLACK_BYTES:
            self.manager.overshoots += 1
            ws.activity.storage_blocked = (
                "This demo workspace wrote more in one request than its storage bound allows (database "
                f"+{db_grown} of {plan.db_growth} bytes, files +{files_grown} of {plan.files} bytes). "
                "This should not happen; to stay safe, further changes to this workspace are refused. "
                "Its data can still be read, or start a new demo workspace."
            )

    def _pending_refusal(self, ws: Workspace, method: str, scope: dict) -> tuple | None:
        """Refuse a state-changing request while an earlier one that outlived its own request still runs.
        (Repeats after it has finished are recognised only through an Idempotency-Key: matching on the
        address alone refused different requests to the same route.)"""
        pending = ws.activity.pending
        if pending is None or method not in MUTATING or pending.done:
            return None  # reads simply wait for the workspace's lock
        return (
            409,
            "previous_request_still_processing",
            "An earlier request to this demo workspace is still being processed and may still complete. "
            "Wait for it, then reload the page; nothing new was started.",
        )

    async def _forward(self, job: _Job, scope, receive, send, method, path, new_cookie, headers) -> None:
        ws = job.ws
        inner_host = f"{LOOPBACK_HOST}:{self.inner_port}"
        inner_headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() not in DROPPED]
        inner_headers.append((b"host", inner_host.encode()))
        if "origin" in headers:
            inner_headers.append((b"origin", f"http://{inner_host}".encode()))
        if method in ("GET", "HEAD") and path in PAGE_PATHS:
            # unlocks the page (the token <meta>), as the offline entry link does; never an API credential
            inner_headers.append((b"cookie", f"{SESSION_COOKIE}={ws.token}".encode()))
        inner = dict(scope)
        inner["headers"] = inner_headers
        inner["server"] = (LOOPBACK_HOST, self.inner_port)
        inner["scheme"] = "http"
        state = {"started": False, "status": None, "detached": False, "held": None}

        async def inner_send(message: dict) -> None:
            if job.idempotency is not None:  # kept for replays, also after a timeout
                if message["type"] == "http.response.start":
                    ctype = dict(message.get("headers", [])).get(b"content-type", b"application/json")
                    job.response_type = ctype.decode("latin-1")
                elif message["type"] == "http.response.body":
                    job.response += message.get("body", b"")
                    if len(job.response) > IDEMPOTENCY_MAX_RESPONSE:
                        job.response_overflow = True
                        job.response.clear()
            if message["type"] == "http.response.start":
                state["status"] = message["status"]  # recorded even when detached: it decides the accounting
            if state["detached"]:
                return  # the visitor's request has already been answered (or is gone)
            if message["type"] == "http.response.start":
                hs = list(message.get("headers", []))
                if new_cookie is not None:
                    hs.append((b"set-cookie", new_cookie))
                if self.https:
                    hs.append((b"strict-transport-security", b"max-age=31536000"))
                message = {**message, "headers": hs}
                if message["status"] >= 500:  # held until the work ends (see below)
                    state["held"] = [message]
                    return
                state["started"] = True
            if state["held"] is not None:
                state["held"].append(message)
                return
            await send(message)

        declared = int(headers["content-length"]) if headers.get("content-length", "").isdigit() else 0
        received = 0

        async def bounded_receive() -> dict:
            """The body as declared and no more (the server already holds the client to Content-Length;
            this keeps the workspace app to it even if that ever changed)."""
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                received += len(chunk)
                if received > declared:
                    return {"type": "http.disconnect"}
                job.body_hash.update(chunk)
                if not message.get("more_body", False):
                    job.body_complete = received == declared
            return message

        if declared == 0:
            job.body_complete = True  # nothing to read
        job.forwarded = True
        task = asyncio.ensure_future(ws.app(inner, bounded_receive, inner_send))
        try:
            done, _ = await asyncio.wait({task}, timeout=self.processing_timeout)
            if not done and state["started"]:
                await asyncio.shield(task)  # the answer is already being sent: let it finish in this request
                done = {task}
        except asyncio.CancelledError:
            state["detached"] = True
            self._hand_off(job, task, state, method, scope)
            raise
        if done:
            exc = None if task.cancelled() else task.exception()
            if _storage_full(exc):
                # SQLite refused to grow the database past this request's cap; the failing transaction was
                # rolled back by SQLite. Reported as what it is, instead of the app's generic 500.
                job.status = 507
                job.response_overflow = True  # replayed by status only
                await self._error(
                    send,
                    507,
                    "storage_limit_reached",
                    "This operation needed more space than this demo workspace has left "
                    f"({self.limits.max_workspace_bytes} bytes in all); the step that ran out of space was "
                    "rolled back.",
                    details={"database_cap_bytes": job.plan.db_cap if job.plan else None},
                )
                return
            for message in state["held"] or []:
                await send(message)
            if exc is not None:
                raise exc  # any other exception in the app propagates; the job finishes without counting
            job.status = state["status"]
            return
        # No answer in time. Cancelling is not safe (a worker thread may be committing), so the work keeps the
        # lock and the reservation until it really ends, and the visitor is told the truth.
        state["detached"] = True
        self._hand_off(job, task, state, method, scope)
        seconds = f"{self.processing_timeout:g}"
        if method in MUTATING:
            await self._error(
                send,
                504,
                "still_processing",
                f"This request is still being processed (it has taken more than {seconds} s) and may still "
                "complete. Do not send it again: reload the page in a minute to see the result.",
                details={"may_complete": True},
            )
        else:
            await self._error(
                send,
                504,
                "processing_timeout",
                f"This request took more than {seconds} s and its answer was discarded; try again shortly.",
            )

    def _hand_off(self, job: _Job, task: asyncio.Future, state: dict, method: str, scope: dict) -> None:
        """Leave the job to `task`: when the work really ends, record its outcome, count it, and release."""
        job.handed_off = True
        pending = None
        if method in MUTATING:
            key = (method, scope["path"], scope.get("query_string", b""))
            pending = PendingRequest(key, self.clock())
            job.ws.activity.pending = pending

        def done(t: asyncio.Future) -> None:
            ok = not t.cancelled() and t.exception() is None
            job.status = state["status"] if ok else None
            if pending is not None:
                pending.done, pending.status, pending.finished_at = True, job.status, self.clock()
            job.finish()

        task.add_done_callback(done)
