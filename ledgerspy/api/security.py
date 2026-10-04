"""Local-only security controls (docs/architecture.md §13).

* Host must be the loopback origin we serve on (defeats DNS rebinding).
* Origin, when sent, must be that same origin; cross-site Fetch Metadata is refused (CSRF).
* Every /api/ route except an explicit public allow-list needs `Authorization: Bearer <session token>`.
  The token is random per launch and is never accepted from a cookie for API calls, so a browser cannot
  attach it automatically to a forged request.
* Security headers on every response, including errors.
"""

import base64
import hashlib
import html
import re
import secrets

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

PUBLIC_API_PATHS = frozenset({"/api/health"})
SESSION_COOKIE = "ledgerspy_session"

_INLINE_HANDLER = re.compile(r"""\son[a-z]+\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)


def inline_handler_hashes(page: str) -> list[str]:
    """CSP hashes of the page's inline event-handler attributes (as the browser sees them, entity-decoded).

    The existing UI still uses inline `onclick=` handlers (removal is P20). Allowing exactly these handlers
    via 'unsafe-hashes' keeps `script-src` free of 'unsafe-inline', so injected markup cannot run script.
    """
    hashes = set()
    for match in _INLINE_HANDLER.finditer(page):
        value = html.unescape(match.group(1) if match.group(1) is not None else match.group(2))
        digest = base64.b64encode(hashlib.sha256(value.encode("utf-8")).digest()).decode("ascii")
        hashes.add(f"'sha256-{digest}'")
    return sorted(hashes)


def build_csp(page: str) -> str:
    script_src = " ".join(["'self'", "'unsafe-hashes'", *inline_handler_hashes(page)])
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src {script_src}",
            # 161 inline style="" attributes remain in the existing UI (P20 moves them to CSS).
            "style-src 'self' 'unsafe-inline'",
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
        await self.app(scope, receive, send_with_headers)
