"""FastAPI application: serves the existing frontend/ and the API from one loopback process."""

from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from ledgerspy import __version__
from ledgerspy.api.routes import anomaly, cases, datasets, forensics, health, quality, risk
from ledgerspy.api.security import SESSION_COOKIE, LocalSecurityMiddleware, build_csp, token_matches
from ledgerspy.cases.audit import local_actor
from ledgerspy.config import EffectiveConfig
from ledgerspy.config.schema import LOOPBACK_HOST
from ledgerspy.paths import FRONTEND_DIR

TOKEN_META = "ledgerspy-session-token"  # noqa: S105 — a <meta> name, not a secret

_LOCKED_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>LedgerSpy</title></head>
<body><p>LedgerSpy is locked. Open the link printed by <code>ledgerspy serve</code> in the terminal.</p>
</body></html>"""


def _inject_token(page: str, token: str) -> str:
    """Give the UI the per-launch API token (for P3+ API calls) without changing frontend/ files."""
    meta = f'<meta name="{TOKEN_META}" content="{token}">'
    head = page.find("<head>")
    if head < 0:
        raise ValueError("frontend/index.html has no <head>")
    insert_at = head + len("<head>")
    return page[:insert_at] + "\n" + meta + page[insert_at:]


def create_app(
    config: EffectiveConfig, db_path: Path, token: str, frontend_dir: Path = FRONTEND_DIR
) -> FastAPI:
    """Build the app. The database must already be initialised (`ledgerspy.cases.db.init_db`)."""
    if len(token) < 32:
        raise ValueError("session token is too short")
    page = (frontend_dir / "index.html").read_text(encoding="utf-8")
    served_page = _inject_token(page, token)

    # No /docs or /redoc: Swagger UI loads assets from a CDN. The OpenAPI JSON is token-protected.
    app = FastAPI(
        title="LedgerSpy", version=__version__, docs_url=None, redoc_url=None, openapi_url="/api/openapi.json"
    )
    app.state.config = config
    app.state.db_path = Path(db_path)
    app.state.workspace = Path(db_path).parent  # evidence/ and tmp/ live next to the database
    app.state.actor = local_actor()

    app.include_router(health.router)
    app.include_router(cases.router)
    app.include_router(quality.router)  # before datasets: GET /api/datasets must not hit /{dataset_id}
    app.include_router(datasets.router)
    app.include_router(forensics.router)
    app.include_router(anomaly.router)
    app.include_router(risk.router)

    @app.get("/", include_in_schema=False)
    def index(request: Request) -> Response:
        # Entry link from `ledgerspy serve`: /?token=… → exchange for an HttpOnly, SameSite=Strict cookie and
        # redirect so the token leaves the address bar and history.
        url_token = request.query_params.get("token")
        if url_token is not None:
            if not token_matches(url_token, token):
                return HTMLResponse(_LOCKED_PAGE, status_code=401)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", path="/")
            return response
        if token_matches(request.cookies.get(SESSION_COOKIE), token):
            return HTMLResponse(served_page)
        return HTMLResponse(_LOCKED_PAGE, status_code=401)

    @app.get("/index.html", include_in_schema=False)
    def index_alias() -> Response:
        return RedirectResponse("/", status_code=307)

    # Static UI assets (css/, js/). They contain no case data; the page itself is gated above.
    app.mount("/", StaticFiles(directory=frontend_dir), name="frontend")

    app.add_middleware(
        LocalSecurityMiddleware, token=token, port=config.settings.server.port, csp=build_csp(page)
    )
    return app


def build_server(app: FastAPI, config: EffectiveConfig) -> uvicorn.Server:
    host = config.settings.server.host
    if host != LOOPBACK_HOST:  # also enforced by the config schema; defence in depth
        raise ValueError(f"refusing to bind to non-loopback address {host!r}")
    return uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=config.settings.server.port,
            proxy_headers=False,  # never trust X-Forwarded-* headers
            server_header=False,
            access_log=False,  # the entry URL carries the token; keep it out of logs
            log_level="info",
        )
    )
