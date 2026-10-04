"""P3: ingestion API — authentication preserved, structured results and errors, no network access."""

import json
import socket

import pytest
from fastapi.testclient import TestClient

from ledgerspy.api.app import create_app
from ledgerspy.cases.db import db_path_for, init_db
from ledgerspy.config import load_config
from tests.backend.conftest import BASE_URL, FIXTURES, PAYSIM, PORT, make_ingest
from tests.backend.xlsx_builder import build_xlsx

OCTET = {"Content-Type": "application/octet-stream"}


def new_case(client, auth):
    return client.post("/api/cases", json={"name": "Ingestion"}, headers=auth).json()["case_id"]


def upload(client, auth, case_id, data: bytes, filename: str, **params):
    query = "&".join(f"{k}={v}" for k, v in {"filename": filename, **params}.items())
    return client.post(f"/api/cases/{case_id}/files?{query}", content=data, headers={**auth, **OCTET})


# ---------- security from P2 still applies ----------


def test_upload_requires_the_session_token(client, auth):
    case_id = new_case(client, auth)
    r = client.post(f"/api/cases/{case_id}/files?filename=a.csv", content=b"x", headers=OCTET)
    assert r.status_code == 401
    assert client.get("/api/datasets/D-01J00000000000000000000000").status_code == 401
    assert client.put("/api/datasets/D-01J00000000000000000000000/mapping", json={}).status_code == 401


def test_cross_origin_upload_is_refused(client, auth):
    case_id = new_case(client, auth)
    r = upload(client, {**auth, "Origin": "http://evil.example"}, case_id, b"Date,A\n1,2\n", "a.csv")
    assert r.status_code == 403


def test_nothing_is_written_for_unauthenticated_uploads(client, auth, db_path):
    case_id = new_case(client, auth)
    client.post(f"/api/cases/{case_id}/files?filename=a.csv", content=PAYSIM.read_bytes(), headers=OCTET)
    assert not (db_path.parent / "evidence").exists()


# ---------- the full flow over HTTP ----------


def test_paysim_flow_over_http(client, auth):
    case_id = new_case(client, auth)
    r = upload(client, auth, case_id, PAYSIM.read_bytes(), "paysim_sample_10k.csv")
    assert r.status_code == 201
    ds = r.json()
    assert ds["status"] == "mapping_required" and ds["row_count"] == 10_127
    assert ds["evidence"]["original_filename"] == "paysim_sample_10k.csv"
    assert ds["mapping_suggestion"]["adapter"]["suggested"] == "paysim"
    url = f"/api/datasets/{ds['dataset_id']}"

    assert client.get(f"{url}/transactions", headers=auth).status_code == 409  # not canonicalised yet
    r = client.put(f"{url}/mapping", json=ds["mapping_suggestion"]["proposed_mapping"], headers=auth)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["rows"] == 10_127 and result["labels"]["columns"] == ["isFlaggedFraud", "isFraud"]

    info = client.get(url, headers=auth).json()
    assert info["status"] == "canonicalised" and info["canonical"]["time_basis"] == "simulation_step"
    assert info["canonical"]["mapping_sha256"] == result["mapping_sha256"]
    page = client.get(f"{url}/transactions?page=2&size=50", headers=auth).json()
    assert page["total"] == 10_127 and [t["source_row"] for t in page["items"]] == list(range(51, 101))
    assert "isFraud" not in json.dumps(page) and "isFlaggedFraud" not in json.dumps(page)
    assert client.put(f"{url}/mapping", json=result | {}, headers=auth).status_code == 422  # not a Mapping
    again = client.put(f"{url}/mapping", json=ds["mapping_suggestion"]["proposed_mapping"], headers=auth)
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_canonicalised"


def test_xlsx_upload_over_http(client, auth):
    case_id = new_case(client, auth)
    rows = [["Date", "Vendor", "Amount", "Dr/Cr"], ["2024-04-01", "Acme", 10.5, "Dr"]]
    r = upload(client, auth, case_id, build_xlsx({"Ledger": rows}), "ledger.xlsx")
    assert r.status_code == 201 and r.json()["evidence"]["format"] == "xlsx"


# ---------- structured errors ----------


def test_rejections_are_structured(client, auth):
    case_id = new_case(client, auth)
    r = upload(client, auth, case_id, (FIXTURES / "ragged.csv").read_bytes(), "ragged.csv")
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "rejected"
    assert body["error"]["code"] == "ragged_row" and "header has 4" in body["error"]["message"]
    assert body["error"]["details"]["source_row"] == 2


@pytest.mark.parametrize(
    ("data", "name", "status", "code"),
    [
        (b"x", "ledger.pdf", 415, "unsupported_format"),
        (b"", "empty.csv", 422, "empty_file"),
        (
            build_xlsx({"S": [["a", "b"]]}, extra_parts={"xl/vbaProject.bin": b"v"}),
            "m.xlsx",
            422,
            "xlsx_macro",
        ),
    ],
)
def test_error_statuses(client, auth, data, name, status, code):
    case_id = new_case(client, auth)
    r = upload(client, auth, case_id, data, name)
    assert r.status_code == status and r.json()["error"]["code"] == code


def test_invalid_mapping_lists_every_error(client, auth):
    case_id = new_case(client, auth)
    ds = upload(client, auth, case_id, (FIXTURES / "ledger_normal.csv").read_bytes(), "l.csv").json()
    bad = {"adapter": "generic_ledger", "fields": {"amount": "isFraud", "posted_at": "Missing"}}
    r = client.put(f"/api/datasets/{ds['dataset_id']}/mapping", json=bad, headers=auth)
    assert r.status_code == 422
    codes = {e["code"] for e in r.json()["error"]["details"]["errors"]}
    assert codes == {"label_columns", "label_column_mapped", "unknown_column"}  # isFraud must stay a label


def test_unknown_ids(client, auth):
    case_id = "C-01J00000000000000000000000"
    r = upload(client, auth, case_id, b"Date,Amount\n2024-01-01,5\n", "a.csv")
    assert r.status_code == 404 and r.json()["error"]["code"] == "case_not_found"
    assert client.get("/api/datasets/D-01J00000000000000000000000", headers=auth).status_code == 404
    assert client.get("/api/datasets/not-an-id", headers=auth).status_code == 422
    assert upload(client, auth, "bad id", b"x", "a.csv").status_code in (404, 422)


def test_kind_other_than_ledger_is_not_yet_supported(client, auth):
    case_id = new_case(client, auth)
    r = upload(client, auth, case_id, (FIXTURES / "ledger_normal.csv").read_bytes(), "l.csv", kind="bank")
    assert r.status_code == 422 and r.json()["error"]["code"] == "kind_not_supported"


def test_oversized_upload_returns_413_and_stores_nothing(tmp_path, token, auth):
    cfg = load_config(
        overrides={
            "workspace": {"path": str(tmp_path / "ws")},
            "server": {"port": PORT},
            "ingest": {"max_file_bytes": 1000},
        }
    )
    db = db_path_for(cfg.settings)
    init_db(db, "tester")
    with TestClient(create_app(cfg, db, token), base_url=BASE_URL) as c:
        case_id = new_case(c, auth)
        r = upload(c, auth, case_id, PAYSIM.read_bytes(), "big.csv")
        assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"
        # Even without a Content-Length header the streamed size is enforced.
        chunks = iter([b"a" * 600, b"b" * 600])
        r = c.post(f"/api/cases/{case_id}/files?filename=s.csv", content=chunks, headers={**auth, **OCTET})
        assert r.status_code == 413
    assert not (db.parent / "evidence").exists()
    assert not list((db.parent / "tmp").glob("*"))


# ---------- 10. no network access during ingestion ----------


def test_ingestion_makes_no_network_calls(tmp_path, monkeypatch):
    """Stronger than the loopback-only guard: ingestion opens no socket and resolves no name at all."""
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("network access during ingestion")

    harness = make_ingest(tmp_path)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    ds = harness.upload(PAYSIM)
    harness.confirm(ds)
    rows = [["Date", "Vendor", "Amount", "Dr/Cr"], ["2024-04-01", "http://203.0.113.9/x", 1, "Dr"]]
    harness.confirm(harness.upload(build_xlsx({"Ledger": rows}), "links.xlsx"))
    assert calls == []
    harness.conn.close()
