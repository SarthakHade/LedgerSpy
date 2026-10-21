"""Fixtures for the hosted-demo tests: one synthetic seed per session, demo instances with an injectable
clock, and an ASGI request helper that drives the front door exactly as an HTTP server would (no network)."""

import asyncio
import re
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path

import pytest

from ledgerspy_demo.frontdoor import COOKIE, FrontDoor
from ledgerspy_demo.frontend import build_demo_frontend
from ledgerspy_demo.seed import build_seed
from ledgerspy_demo.settings import DemoSettings
from ledgerspy_demo.workspace import WorkspaceManager

HOST = "demo.example.org"
ORIGIN = f"https://{HOST}"


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def demo_env(data_dir: Path, **limits: int | str) -> dict[str, str]:
    env = {
        "LEDGERSPY_MODE": "demo",
        "LEDGERSPY_DEMO_SESSION_KEY": "test-only-session-key-0123456789abcdefghijkl",
        "LEDGERSPY_DEMO_DATA_DIR": str(data_dir),
        "LEDGERSPY_DEMO_PUBLIC_ORIGIN": ORIGIN,
    }
    for k, v in limits.items():
        env[k if k.startswith(("LEDGERSPY_", "PORT")) else f"LEDGERSPY_DEMO_{k.upper()}"] = str(v)
    return env


@pytest.fixture(scope="session")
def seed_dir(tmp_path_factory) -> Path:
    data = tmp_path_factory.mktemp("seed-host")
    settings = DemoSettings.from_env(demo_env(data))
    manager = WorkspaceManager(settings, data / "seed", data / "frontend-unused")
    build_seed(data / "seed", manager.config_for)
    return data / "seed"


@dataclass
class Demo:
    door: FrontDoor
    manager: WorkspaceManager
    settings: DemoSettings
    clock: Clock
    data: Path


@pytest.fixture
def make_demo(tmp_path, seed_dir):
    def make(**limits) -> Demo:
        data = tmp_path / f"demo-{len(list(tmp_path.iterdir()))}"
        data.mkdir()
        settings = DemoSettings.from_env(demo_env(data, **limits))
        clock = Clock()
        frontend = build_demo_frontend(data / "frontend", settings.limits.retention_days, settings.persistent)
        manager = WorkspaceManager(settings, seed_dir, frontend, clock=clock)
        return Demo(FrontDoor(settings, manager, clock=clock), manager, settings, clock, data)

    return make


@dataclass
class Response:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        return next((v for k, v in self.headers if k == name), None)

    def all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k == name]

    def json(self):
        import json

        return json.loads(self.body)


async def request(
    app,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    client: tuple[str, int] = ("203.0.113.10", 50000),
    host: str = HOST,
) -> Response:
    path_only, _, query = path.partition("?")
    hs = [(b"host", host.encode())] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if body and "content-length" not in {k.lower() for k in (headers or {})}:
        hs.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path_only,
        "raw_path": path_only.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": hs,
        "client": client,
        "server": (host, 443),
    }
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)  # no disconnect while the app is working
        return {"type": "http.disconnect"}

    out = {"status": None, "headers": [], "body": b""}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            out["headers"] = [(k.decode().lower(), v.decode()) for k, v in message.get("headers", [])]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    await app(scope, receive, send)
    return Response(out["status"], out["headers"], out["body"])


@dataclass
class Visitor:
    """A browser: keeps the demo cookie and the page's API token; a fixed client address."""

    app: object
    client: tuple[str, int] = ("203.0.113.10", 50000)
    cookie: str | None = None
    token: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    async def call(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        auth: bool = True,
        json_body=None,
    ) -> Response:
        import json

        hs = dict(self.extra)
        if self.cookie:
            hs["cookie"] = f"{COOKIE}={self.cookie}"
        if auth and self.token and path.startswith("/api/"):
            hs["authorization"] = f"Bearer {self.token}"
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hs["content-type"] = "application/json"
        hs.update(headers or {})
        r = await request(self.app, method, path, hs, body, client=self.client)
        for value in r.all("set-cookie"):
            jar = SimpleCookie()
            jar.load(value)
            if COOKIE in jar:
                self.cookie = jar[COOKIE].value
        return r

    async def open(self) -> Response:
        """As a browser does: without a workspace, the landing page's form posts to /demo/workspace/new (the
        only way a workspace is created), then the page loads. A refused creation is returned as it is."""
        if self.cookie is None:
            r = await self.call("POST", "/demo/workspace/new", headers={"origin": ORIGIN})
            if r.status != 303:
                return r
        r = await self.call("GET", "/")
        m = re.search(rb'<meta name="ledgerspy-session-token" content="([^"]+)">', r.body)
        if m:
            self.token = m.group(1).decode()
        return r
