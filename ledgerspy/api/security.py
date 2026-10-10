"""Local-only security controls (docs/architecture.md §13).

* Host must be the loopback origin we serve on (defeats DNS rebinding).
* Origin, when sent, must be that same origin; cross-site Fetch Metadata is refused (CSRF).
* Every /api/ route except an explicit public allow-list needs `Authorization: Bearer <session token>`.
  The token is random per launch and is never accepted from a cookie for API calls, so a browser cannot
  attach it automatically to a forged request.
* Security headers on every response, including errors.
* P20: a strict CSP (`script-src 'self'`, `style-src 'self'`: no inline script, handler or style attribute is
  allowed; the page refuses to be served if it contains one), and a bound on request bodies.
"""

import base64
import hashlib
import html
import re
import secrets

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ledgerspy.api.limits import MAX_BODY_BYTES

PUBLIC_API_PATHS = frozenset({"/api/health"})
SESSION_COOKIE = "ledgerspy_session"

_INLINE_HANDLER = re.compile(r"""\son[a-z]+\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_INLINE_STYLE = re.compile(r"""\sstyle\s*=""", re.IGNORECASE)
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>", re.IGNORECASE)
_STYLE_ELEMENT = re.compile(r"<style[\s>]", re.IGNORECASE)
# The file upload streams to disk under its own limit (ingest.max_file_bytes); every other body is
# bounded here.
_STREAMED_UPLOAD = re.compile(r"^/api/cases/[^/]+/files$")


def inline_handler_hashes(page: str) -> list[str]:
    """CSP hashes of the page's inline event-handler attributes (as the browser sees them, entity-decoded).

    Kept for diagnostics and tests: P20 removed every handler from the UI and the CSP allows none.
    """
    hashes = set()
    for match in _INLINE_HANDLER.finditer(page):
        value = html.unescape(match.group(1) if match.group(1) is not None else match.group(2))
        digest = base64.b64encode(hashlib.sha256(value.encode("utf-8")).digest()).decode("ascii")
        hashes.add(f"'sha256-{digest}'")
    return sorted(hashes)


def inline_markup(page: str) -> list[str]:
    """What a strict CSP would block: inline handlers, style attributes, inline scripts, <style> elements."""
    found = []
    if _INLINE_HANDLER.search(page):
        found.append("inline event handler")
    if _INLINE_STYLE.search(page):
        found.append("style attribute")
    if _INLINE_SCRIPT.search(page):
        found.append("inline <script>")
    if _STYLE_ELEMENT.search(page):
        found.append("<style> element")
    return found


def build_csp(page: str | None = None) -> str:
    """The page's CSP. P20: strict for scripts and styles; a page with inline markup is refused."""
    if page is not None and (bad := inline_markup(page)):
        raise ValueError(f"frontend/index.html contains {', '.join(bad)}; the strict CSP would block it")
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self'",
            "font-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


def security_headers(csp: str) -> dict[str, str]:
    return {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        "Cache-Control": "no-store",
    }


def token_matches(candidate: str | None, token: str) -> bool:
    return candidate is not None and secrets.compare_digest(candidate.encode(), token.encode())


class LocalSecurityMiddleware:
    """Pure ASGI middleware applied in front of the whole app (API and static UI)."""

    def __init__(self, app: ASGIApp, *, token: str, port: int, csp: str) -> None:
        self.app = app
        self.token = token
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}
        self.headers = security_headers(csp)

    def _reject(self, scope: Scope) -> tuple[int, str] | None:
        headers = Headers(scope=scope)
        if headers.get("host", "").lower() not in self.allowed_hosts:
            return 400, "invalid Host header"
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in self.allowed_origins:
            return 403, "cross-origin request refused"
        if headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return 403, "cross-site request refused"
        path = scope["path"]
        if (path == "/api" or path.startswith("/api/")) and path not in PUBLIC_API_PATHS:
            scheme, _, credential = headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not token_matches(credential.strip(), self.token):
                return 401, "session token required"
        declared = headers.get("content-length")
        if declared is not None and not _STREAMED_UPLOAD.match(path):
            if not declared.isdigit():
                return 400, "invalid Content-Length"
            if int(declared) > MAX_BODY_BYTES:
                return 413, f"request body larger than {MAX_BODY_BYTES} bytes"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # no websockets
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    response_headers[name] = value
            await send(message)

        rejection = self._reject(scope)
        if rejection is not None:
            status, detail = rejection
            extra = {"WWW-Authenticate": "Bearer"} if status == 401 else None
            await JSONResponse({"detail": detail}, status_code=status, headers=extra)(
                scope, receive, send_with_headers
            )
            return
        if _STREAMED_UPLOAD.match(scope["path"]) or "content-length" in Headers(scope=scope):
            # the upload enforces its own limit; a declared length was checked above (the server holds the
            # client to it)
            await self.app(scope, receive, send_with_headers)
            return
        # No declared length (chunked): read the body here, up to the limit, then hand it on unchanged.
        body, more = bytearray(), True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(scope, receive, send_with_headers)  # disconnect: let the app see it
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > MAX_BODY_BYTES:
                await JSONResponse(
                    {"detail": f"request body larger than {MAX_BODY_BYTES} bytes"}, status_code=413
                )(scope, receive, send_with_headers)
                return
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send_with_headers)
