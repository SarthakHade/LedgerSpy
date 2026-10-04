import secrets
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ledgerspy.api.app import create_app
from ledgerspy.cases.db import db_path_for, init_db
from ledgerspy.config import EffectiveConfig, load_config

PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}"


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def config(tmp_path: Path) -> EffectiveConfig:
    return load_config(overrides={"workspace": {"path": str(tmp_path / "ws")}, "server": {"port": PORT}})


@pytest.fixture
def db_path(config: EffectiveConfig) -> Path:
    path = db_path_for(config.settings)
    init_db(path, "tester")
    return path


@pytest.fixture
def token() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture
def client(config: EffectiveConfig, db_path: Path, token: str) -> TestClient:
    with TestClient(create_app(config, db_path, token), base_url=BASE_URL) as c:
        yield c


@pytest.fixture
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
