"""P4 API: integrity reports, issue paging/traceability, decisions, normalise, revert (token-protected)."""

import socket

import pytest

from tests.backend.conftest import QUALITY, explicit_mapping, make_ingest

OCTET = {"Content-Type": "application/octet-stream"}


def canonical_dataset(client, auth, fixture="normalization.csv", period=None):
    body = {"name": "Quality"}
    if period:
        body |= {"period_start": period[0], "period_end": period[1]}
    case_id = client.post("/api/cases", json=body, headers=auth).json()["case_id"]
    ds = client.post(
        f"/api/cases/{case_id}/files?filename={fixture}",
        content=(QUALITY / fixture).read_bytes(),
        headers={**auth, **OCTET},
    ).json()
    r = client.put(f"/api/datasets/{ds['dataset_id']}/mapping", json=explicit_mapping(ds), headers=auth)
    assert r.status_code == 200, r.text
    return ds["dataset_id"]


def test_p4_endpoints_require_the_token(client, auth):
    dsid = canonical_dataset(client, auth)
    for method, path in [
        ("get", "/api/datasets"),
        ("post", f"/api/datasets/{dsid}/integrity"),
        ("get", f"/api/datasets/{dsid}/integrity"),
        ("get", f"/api/datasets/{dsid}/integrity/issues"),
        ("get", f"/api/datasets/{dsid}/fixes"),
        ("post", f"/api/datasets/{dsid}/fixes/decisions"),
        ("post", f"/api/datasets/{dsid}/normalize"),
        ("get", f"/api/datasets/{dsid}/transformations"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path


def test_integrity_flow_over_http(client, auth):
    dsid = canonical_dataset(client, auth)
    url = f"/api/datasets/{dsid}"
    r = client.get(f"{url}/integrity", headers=auth)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_run"

    report = client.post(f"{url}/integrity", headers=auth).json()
    assert report["run_id"].startswith("Q-") and report["readiness"]["score"] == 92.5
    assert client.get(f"{url}/integrity", headers=auth).json()["run_id"] == report["run_id"]
    listed = client.get("/api/datasets", headers=auth).json()["items"]
    assert [(d["dataset_id"], d["latest_readiness"]) for d in listed] == [(dsid, 92.5)]

    issues = client.get(f"{url}/integrity/issues?check=needs_normalization", headers=auth).json()
    assert issues["total"] == 5 and all(i["fix_status"] == "proposed" for i in issues["items"])
    one = issues["items"][0]
    page = client.get(f"{url}/transactions?size=500", headers=auth).json()["items"]
    txn = next(t for t in page if t["txn_id"] == one["txn_id"])  # issue → its transaction
    assert txn["source_row"] == one["source_row"]

    fixes = client.get(f"{url}/fixes?status=proposed", headers=auth).json()["items"]
    assert len(fixes) == 6
    reject = next(f for f in fixes if f["field"] == "payee_raw")
    decisions = [{"fix_id": f["fix_id"], "decision": "accept"} for f in fixes if f is not reject]
    decisions.append({"fix_id": reject["fix_id"], "decision": "reject", "reason": "keep vendor spelling"})
    r = client.post(f"{url}/fixes/decisions", json={"decisions": decisions}, headers=auth)
    assert r.status_code == 200 and len(r.json()["items"]) == 6

    result = client.post(f"{url}/normalize", headers=auth).json()
    assert len(result["applied"]) == 5 and result["conflicts"] == []
    # only INFO issues remain (the rejected payee spacing, the absent document date): ready
    assert result["readiness"]["score"] == 100.0 and result["readiness"]["status"] == "ready"
    log = client.get(f"{url}/transformations", headers=auth).json()["items"]
    assert len(log) == 5 and {t["kind"] for t in log} == {"apply"}

    normalized = client.get(f"{url}/transactions?size=500", headers=auth).json()["items"]
    canonical = client.get(f"{url}/transactions?size=500&view=canonical", headers=auth).json()["items"]
    row3n, row3c = normalized[2], canonical[2]
    assert row3n["reference"] == "RCPT-0077" and row3c["reference"] == "RCPT​-0077"
    assert row3n["normalized_fields"] == ["reference", "txn_type"] and "normalized_fields" not in row3c
    assert normalized[0]["payee_raw"] == "Acme  Supplies Ltd"  # rejected → unchanged

    t = next(x for x in log if x["field"] == "reference")
    r = client.post(
        f"{url}/transformations/{t['transformation_id']}/revert", json={"reason": "check"}, headers=auth
    )
    assert r.status_code == 200 and r.json()["restored_value"] == "RCPT​-0077"
    again = client.post(
        f"{url}/transformations/{t['transformation_id']}/revert", json={"reason": "x"}, headers=auth
    )
    assert again.status_code == 409


def test_invalid_decisions_are_rejected(client, auth):
    dsid = canonical_dataset(client, auth, "ambiguous.csv")
    client.post(f"/api/datasets/{dsid}/integrity", headers=auth)
    fixes = client.get(f"/api/datasets/{dsid}/fixes", headers=auth).json()["items"]
    ambiguous = next(f for f in fixes if f["proposed_value"] is None)
    r = client.post(
        f"/api/datasets/{dsid}/fixes/decisions",
        json={"decisions": [{"fix_id": ambiguous["fix_id"], "decision": "accept"}]},
        headers=auth,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_decisions"
    assert r.json()["error"]["details"]["errors"][0]["candidates"] == ambiguous["candidates"]
    bad = client.post(
        f"/api/datasets/{dsid}/fixes/decisions",
        json={"decisions": [{"fix_id": "X-0000000000000000", "decision": "accept"}]},
        headers=auth,
    )
    assert bad.status_code == 422
    assert (
        client.post(f"/api/datasets/{dsid}/fixes/decisions", json={"decisions": []}, headers=auth).status_code
        == 422
    )


def test_integrity_on_an_unmapped_dataset_is_refused(client, auth):
    case_id = client.post("/api/cases", json={"name": "x"}, headers=auth).json()["case_id"]
    ds = client.post(
        f"/api/cases/{case_id}/files?filename=clean.csv",
        content=(QUALITY / "clean.csv").read_bytes(),
        headers={**auth, **OCTET},
    ).json()
    r = client.post(f"/api/datasets/{ds['dataset_id']}/integrity", headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_canonicalised"
    assert (
        client.post("/api/datasets/D-01J00000000000000000000000/integrity", headers=auth).status_code == 404
    )


def test_period_from_the_case_is_used(client, auth):
    dsid = canonical_dataset(client, auth, "out_of_period.csv", period=("2024-04-01", "2024-04-30"))
    report = client.post(f"/api/datasets/{dsid}/integrity", headers=auth).json()
    assert report["summary"]["issues_by_check"]["out_of_period"] == 2


def test_quality_makes_no_network_calls(tmp_path, monkeypatch):
    from ledgerspy.normalize import service as norm
    from ledgerspy.quality.engine import run_integrity
    from tests.backend.conftest import PAYSIM, load_quality_fixture

    harness = make_ingest(tmp_path)
    ds = harness.upload(PAYSIM)
    harness.confirm(ds)
    other = load_quality_fixture(harness, "normalization.csv")
    calls = []

    def refuse(*a, **k):
        calls.append(a)
        raise AssertionError("network access")

    for target, name in [
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "getaddrinfo"),
        (socket, "create_connection"),
    ]:
        monkeypatch.setattr(target, name, refuse)
    run_integrity(harness.conn, harness.cfg, ds["dataset_id"], "t")
    run_integrity(harness.conn, harness.cfg, other["dataset_id"], "t")
    fixes = norm.list_fixes(harness.conn, other["dataset_id"])
    norm.decide(
        harness.conn, other["dataset_id"], [{"fix_id": f["fix_id"], "decision": "accept"} for f in fixes], "t"
    )
    norm.apply_accepted(harness.conn, other["dataset_id"], "t")
    assert calls == []
    harness.conn.close()


@pytest.mark.parametrize("view", ["normalized", "canonical"])
def test_transactions_view_parameter(client, auth, view):
    dsid = canonical_dataset(client, auth, "clean.csv")
    r = client.get(f"/api/datasets/{dsid}/transactions?view={view}", headers=auth)
    assert r.status_code == 200 and r.json()["view"] == view
    assert client.get(f"/api/datasets/{dsid}/transactions?view=raw", headers=auth).status_code == 422
