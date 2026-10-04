"""The real uvicorn server binds to 127.0.0.1 only."""

import http.client
import secrets
import threading
import time

import pytest

from ledgerspy.api.app import build_server, create_app
from ledgerspy.config import load_config
from ledgerspy.config.schema import LedgerSpyConfig
from tests.backend.conftest import free_loopback_port

TOKEN = secrets.token_urlsafe(32)


@pytest.fixture
def running_server(tmp_path, db_path):
    port = free_loopback_port()
    cfg = load_config(overrides={"server": {"port": port}, "workspace": {"path": str(tmp_path / "ws")}})
    server = build_server(create_app(cfg, db_path, TOKEN), cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield server, port
    server.should_exit = True
    thread.join(timeout=10)


def test_server_listens_on_loopback_only(running_server):
    server, port = running_server
    bound = [sock.getsockname() for s in server.servers for sock in s.sockets]
    assert bound, "no listening sockets"
    assert all(addr[0] == "127.0.0.1" and addr[1] == port for addr in bound), bound


def test_server_answers_on_loopback(running_server):
    _, port = running_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/health")
    response = conn.getresponse()
    assert response.status == 200
    assert response.getheader("X-Frame-Options") == "DENY"
    assert response.getheader("Server") is None  # no server banner
    conn.close()


def test_build_server_refuses_non_loopback_even_if_validation_is_bypassed(db_path):
    cfg = load_config()
    forged = cfg.settings.model_copy(
        update={"server": cfg.settings.server.model_copy(update={"host": "0.0.0.0"})}
    )
    assert isinstance(forged, LedgerSpyConfig)
    app = create_app(cfg, db_path, secrets.token_urlsafe(32))
    with pytest.raises(ValueError, match="non-loopback"):
        build_server(app, type(cfg)(settings=forged, sha256=cfg.sha256, source=cfg.source))


def test_concurrent_requests_on_a_real_server(running_server):
    """Regression: parallel API requests (as the integrity dashboard makes) must not hit SQLite's
    same-thread check when FastAPI runs a dependency's setup and teardown on different threads."""
    from concurrent.futures import ThreadPoolExecutor

    _, port = running_server

    def call(_):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "GET", "/api/cases/C-01J00000000000000000000000", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        status = conn.getresponse().status
        conn.close()
        return status

    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(pool.map(call, range(60)))
    assert statuses == [404] * 60  # every request reached the DB and closed cleanly
