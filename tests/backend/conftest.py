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


# ---------------------------------------------------------------------------------------------- P3 ingestion

FIXTURES = Path(__file__).parent / "fixtures" / "ingest"
PAYSIM = Path(__file__).resolve().parents[2] / "data" / "raw" / "paysim_sample_10k.csv"


class Ingest:
    """Service-level ingestion harness on a fresh workspace (no HTTP)."""

    def __init__(self, cfg: EffectiveConfig, root: Path) -> None:
        from ledgerspy.cases import store
        from ledgerspy.cases.db import connect

        self.cfg = cfg
        self.workspace = root
        init_db(root / "ledgerspy.db", "tester")
        self.conn = connect(root / "ledgerspy.db")
        self.case_id = store.create_case(self.conn, name="Test case", actor="tester")["case_id"]

    def upload(self, source: Path | bytes, filename: str | None = None, kind: str = "ledger", **kw) -> dict:
        from ledgerspy.cases import evidence
        from ledgerspy.ingest import service

        if isinstance(source, bytes):
            path = self.workspace / "incoming" / (filename or "upload.bin")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source)
            source = path
        staged = evidence.stage_file(source, self.workspace, self.cfg.settings.ingest.max_file_bytes)
        return service.ingest_upload(
            self.conn,
            self.cfg,
            self.workspace,
            self.case_id,
            staged,
            filename or source.name,
            kind,
            "tester",
            **kw,
        )

    def confirm(self, dataset: dict, mapping: dict | None = None) -> dict:
        from ledgerspy.ingest import service
        from ledgerspy.ingest.mapping import Mapping

        m = Mapping(**(mapping if mapping is not None else dataset["mapping_suggestion"]["proposed_mapping"]))
        return service.confirm_mapping(
            self.conn, self.cfg, self.workspace, dataset["dataset_id"], m, "tester"
        )

    def transactions(self, dataset_id: str) -> list[dict]:
        import json

        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE dataset_id = ? ORDER BY source_row", (dataset_id,)
        ).fetchall()
        return [{k: (json.loads(r[k]) if k == "extra" else r[k]) for k in r.keys()} for r in rows]  # noqa: SIM118


def make_ingest(tmp_path: Path, **overrides) -> Ingest:
    root = tmp_path / "ws"
    cfg = load_config(overrides={"workspace": {"path": str(root)}, **overrides})
    return Ingest(cfg, root)


@pytest.fixture
def ingest(tmp_path: Path) -> Ingest:
    harness = make_ingest(tmp_path)
    yield harness
    harness.conn.close()


# ---------------------------------------------------------------------------------------------- P4 quality

QUALITY = Path(__file__).parent / "fixtures" / "quality"
STANDARD_COLUMNS = {
    "Date": "posted_at",
    "Invoice Date": "document_date",
    "Account": "account",
    "Vendor": "payee_raw",
    "Description": "description",
    "Invoice No": "reference",
    "Type": "txn_type",
    "Amount": "amount",
    "Dr/Cr": "direction",
    "Currency": "currency",
}


def explicit_mapping(ds: dict) -> dict:
    """The mapping an analyst confirms for the quality fixtures (independent of what was proposed)."""
    header = [c["name"] for c in ds["profile"]["columns"]]
    fields = {STANDARD_COLUMNS[h]: h for h in header if h in STANDARD_COLUMNS}
    options = {"decimal_separator": ".", "date_format": "%Y-%m-%d"}
    if "document_date" in fields:
        options["document_date_format"] = "%Y-%m-%d"
    return {
        "adapter": "generic_ledger",
        "fields": fields,
        "options": options,
        "label_columns": ds["mapping_suggestion"]["label_columns"],
    }


def load_quality_fixture(harness: "Ingest", name: str, period: tuple[str, str] | None = None) -> dict:
    """Upload + confirm a quality fixture into a (new) case, optionally with an audit period."""
    from datetime import date

    from ledgerspy.cases import store

    if period:
        harness.case_id = store.create_case(
            harness.conn,
            name=f"Case {name}",
            actor="tester",
            period_start=date.fromisoformat(period[0]),
            period_end=date.fromisoformat(period[1]),
        )["case_id"]
    ds = harness.upload(QUALITY / name)
    harness.confirm(ds, explicit_mapping(ds))
    return ds
