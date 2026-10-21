"""Stage 2 of the hosted demo: isolated, ephemeral visitor workspaces (docs/demo/public-demo.md).

Drives the real front door and the real, unchanged LedgerSpy workspace apps through ASGI (no network). Run
with `python -m pytest tests/demo`.
"""

import asyncio
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

import pytest

from ledgerspy.cache import NamespacedLRU, forget_namespace, namespace, namespace_for_path
from ledgerspy.canonical.ids import new_ulid
from ledgerspy.cases.db import connect
from ledgerspy.errors import LedgerSpyError
from ledgerspy.graph import service as graph_service
from ledgerspy.reconciliation import service as recon
from ledgerspy.resolution import service as resolution_service
from ledgerspy.risk import service as risk_service
from ledgerspy.supervised import inference
from ledgerspy_demo import allowances
from ledgerspy_demo.frontend import BANNER_TEXT
from ledgerspy_demo.identity import new_workspace_id, sign_cookie
from ledgerspy_demo.seed import remove_tree
from ledgerspy_demo.workspace import LOST_REASON, tree_bytes
from tests.demo.conftest import ORIGIN, Visitor, request

REPO = Path(__file__).resolve().parents[2]
LEDGER = REPO / "data" / "synthetic" / "reference" / "SYNTHETIC_ledger.csv"


def run(coro):
    return asyncio.run(coro)


def small_csv(rows: int, tag: str = "") -> bytes:
    """The first `rows` synthetic ledger rows; a tag goes into the last row's Description."""
    lines = LEDGER.read_bytes().decode("utf-8").splitlines(keepends=True)[: rows + 1]
    if tag:
        cells = lines[-1].rstrip("\r\n").split(",")
        cells[10] = f"{cells[10]} {tag}"  # Description (no commas or quotes in the file's first rows)
        lines[-1] = ",".join(cells) + "\n"
    return "".join(lines).encode("utf-8")


def wid_of(v: Visitor) -> str:
    return v.cookie.split(".")[0]


async def seed_ids(v: Visitor) -> tuple[str, str]:
    items = (await v.call("GET", "/api/datasets")).json()["items"]
    ledger = next(d["dataset_id"] for d in items if d["kind"] == "ledger")
    bank = next(d["dataset_id"] for d in items if d["kind"] == "bank_statement")
    return ledger, bank


async def upload(v: Visitor, case_id: str, body: bytes, name: str = "extra.csv", kind: str = "ledger"):
    return await v.call("POST", f"/api/cases/{case_id}/files?filename={name}&kind={kind}", body=body)


# --------------------------------------------------------------------------------------- identity / cookie


def test_an_anonymous_visit_gets_a_landing_page_and_only_a_post_creates_a_workspace(make_demo):
    d = make_demo()

    async def go():
        for method, path in (("GET", "/"), ("HEAD", "/"), ("GET", "/index.html")):
            r = await request(d.door, method, path)
            assert r.status == 200 and r.header("set-cookie") is None
            assert "no-store" in r.header("cache-control")
            if method == "GET":
                page = r.body.decode()
                assert BANNER_TEXT in page and 'action="/demo/workspace/new"' in page
                assert "<script" not in page and "W-" not in page
        assert d.manager.workspace_ids_on_disk() == []  # anonymous page views allocate nothing
        r = await request(d.door, "POST", "/demo/workspace/new", {"origin": ORIGIN})
        assert r.status == 303 and r.header("location") == "/"  # no workspace ID in the redirect
        assert b"W-" not in r.body and "W-" not in r.header("location")
        cookie = r.header("set-cookie")
        for attr in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", f"Max-Age={7 * 86400}"):
            assert attr in cookie
        assert r.header("strict-transport-security") and "no-store" in r.header("cache-control")
        assert len(d.manager.workspace_ids_on_disk()) == 1
        v = Visitor(d.door, cookie=cookie.split(";")[0].split("=", 1)[1])
        page = await v.open()  # with its cookie: the app page, no second workspace
        assert page.status == 200 and v.token and BANNER_TEXT in page.body.decode()
        assert "not saved across service restarts" in page.body.decode()
        csp = page.header("content-security-policy")
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp
        assert page.header("set-cookie") is None and len(d.manager.workspace_ids_on_disk()) == 1

    run(go())


def test_the_api_needs_the_workspace_cookie_and_that_workspaces_token(make_demo):
    d = make_demo()

    async def go():
        stranger = Visitor(d.door)
        r = await stranger.call("GET", "/api/datasets")
        assert r.status == 401 and r.json()["error"]["code"] == "no_demo_workspace"
        assert d.manager.workspace_ids_on_disk() == []  # an API call never creates a workspace
        a = Visitor(d.door, client=("198.51.100.1", 1))
        await a.open()
        assert (await a.call("GET", "/api/datasets", auth=False)).status == 401  # the inner token check
        assert (await a.call("GET", "/api/openapi.json", auth=False)).status == 401
        assert (await a.call("GET", "/api/datasets")).status == 200
        b = Visitor(d.door, client=("198.51.100.2", 1))
        await b.open()
        r = await b.call("GET", "/api/datasets", headers={"authorization": f"Bearer {a.token}"})
        assert r.status == 401  # A's token is not valid in B's workspace

    run(go())


def test_forged_tampered_malformed_and_expired_cookies_are_never_used(make_demo):
    d = make_demo()
    key = d.settings.session_key
    retention = d.manager.retention_seconds

    async def go():
        a = Visitor(d.door)
        await a.open()
        wid, issued, mac = a.cookie.split(".")
        other = new_workspace_id()
        bad = [
            f"{wid}.{issued}.{'0' * 64}",  # tampered MAC
            f"{other}.{issued}.{mac}",  # another ID with A's MAC
            f"{wid}.{int(issued) + 5}.{mac}",  # tampered issue time
            sign_cookie(b"another-key-another-key-another-key", wid, int(issued)),  # wrong key
            sign_cookie(key, wid, int(d.clock.now) - retention - 10),  # expired
            "../../etc/passwd",
            f"{wid}.{issued}",
            "",
        ]
        for value in bad:
            v = Visitor(d.door, cookie=value, token=a.token, client=("198.51.100.9", 1))
            r = await v.call("GET", "/api/datasets")
            assert r.status == 401 and r.json()["error"]["code"] == "no_demo_workspace", value
        v = Visitor(d.door, cookie=f"{wid}.{issued}.{'0' * 64}", client=("198.51.100.8", 1))
        r = await v.call("GET", "/")  # a page visit with a bad cookie: the landing page, never A's workspace
        assert r.status == 200 and b"/demo/workspace/new" in r.body and r.header("set-cookie") is None
        assert len(d.manager.workspace_ids_on_disk()) == 1  # and nothing allocated
        v.cookie = None
        await v.open()  # an explicit new workspace is a NEW one
        assert wid_of(v) != wid and len(d.manager.workspace_ids_on_disk()) == 2
        d.clock.now += retention + 1  # A's own cookie expires
        assert (await a.call("GET", "/api/datasets")).status == 401

    run(go())


# ------------------------------------------------------------------------------------------- perimeter


def test_host_origin_and_fetch_metadata_are_checked_at_the_front_door(make_demo):
    d = make_demo()

    async def go():
        assert (await request(d.door, "GET", "/", host="evil.example.org")).status == 400
        assert (await request(d.door, "GET", "/", host="127.0.0.1:8765")).status == 400  # the inner Host
        a = Visitor(d.door)
        await a.open()
        r = await a.call("GET", "/api/datasets", headers={"origin": "https://evil.example.org"})
        assert r.status == 403
        r = await a.call(
            "POST", "/api/cases", json_body={"name": "x"}, headers={"sec-fetch-site": "cross-site"}
        )
        assert r.status == 403
        nav = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
        r = await request(d.door, "GET", "/", nav, client=("198.51.100.3", 1))
        assert r.status == 200 and b"url=/" in r.body and r.header("set-cookie") is None
        assert len(d.manager.workspace_ids_on_disk()) == 1  # the bounce created nothing
        r = await request(d.door, "GET", "/", {"sec-fetch-site": "cross-site"})
        assert r.status == 403
        r = await request(d.door, "POST", "/demo/workspace/new")  # no Origin
        assert r.status == 403
        for name in ("content-security-policy", "x-frame-options", "cache-control"):
            assert r.header(name)  # front-door responses carry the security headers too

    run(go())


def test_forwarded_headers_are_untrusted_unless_configured(make_demo):
    async def visits(door, headers_list):
        out = []
        for h in headers_list:
            r = await request(
                door, "POST", "/demo/workspace/new", {**h, "origin": ORIGIN}, client=("10.0.0.5", 1)
            )
            out.append(r.status)
        return out

    d = make_demo(new_workspaces_per_hour_per_client=2)  # default: 0 trusted proxies
    spoofed = [{"x-forwarded-for": f"198.51.100.{i}"} for i in range(2)] + [
        {"x-real-ip": "198.51.100.99"},
        {"forwarded": "for=198.51.100.98"},
    ]
    assert run(visits(d.door, spoofed)) == [303, 303, 429, 429]  # spoofing does not get a fresh quota

    d = make_demo(new_workspaces_per_hour_per_client=2, trusted_proxy_hops=1)
    left_spoof = [{"x-forwarded-for": f"6.6.6.{i}, 198.51.100.7"} for i in range(3)]
    assert run(visits(d.door, left_spoof)) == [303, 303, 429]  # only the proxy-appended entry counts
    real = [{"x-forwarded-for": f"198.51.100.{i}"} for i in range(3)]
    assert run(visits(d.door, real)) == [303, 303, 303]


# ------------------------------------------------------------------------------------------- separation


def test_workspaces_are_separated_across_every_route_family(make_demo):
    d = make_demo(requests_per_minute=500)  # B makes ~60 probe calls

    async def go():
        a = Visitor(d.door, client=("198.51.100.20", 1))
        b = Visitor(d.door, client=("198.51.100.21", 1))
        await a.open()
        await b.open()
        led, bank = await seed_ids(a)
        case = (await a.call("POST", "/api/cases", json_body={"name": "A only"})).json()["case_id"]
        up = (await upload(a, case, small_csv(20, "A"), "a-private.csv")).json()
        ds = up["dataset_id"]
        assert (
            await a.call(
                "PUT", f"/api/datasets/{ds}/mapping", json_body=up["mapping_suggestion"]["proposed_mapping"]
            )
        ).status == 200
        assert (await a.call("POST", f"/api/datasets/{ds}/integrity")).status == 201
        txn = (await a.call("GET", f"/api/datasets/{ds}/transactions?size=1")).json()["items"][0]["txn_id"]
        inv = (
            await a.call(
                "POST",
                "/api/investigations",
                json_body={
                    "case_id": case,
                    "title": "A secret",
                    "links": [
                        {
                            "link_type": "transaction",
                            "dataset_id": ds,
                            "ref_id": txn,
                            "reason": "private lead",
                        }
                    ],
                },
            )
        ).json()
        iid = inv["investigation_id"]
        assert (
            await a.call("POST", f"/api/investigations/{iid}/notes", json_body={"body": "A note"})
        ).status == 201
        rep = (
            await a.call(
                "POST",
                "/api/reports",
                json_body={"report_type": "case_investigation", "investigation_ids": [iid]},
            )
        ).json()["report_id"]
        su = (
            await a.call("GET", f"/api/reconciliation/setup?ledger_dataset_id={led}&bank_dataset_id={bank}")
        ).json()
        code = {"6964": "LVE", "2401": "LVC"}
        scopes = [
            {
                "account_key": x["account_key"],
                "ledger_column": "Company Code",
                "ledger_value": code[x["account"][-4:]],
            }
            for x in su["bank"]["accounts"]
        ]
        run_a = await a.call(
            "POST",
            "/api/reconciliation/runs",
            json_body={
                "ledger_dataset_id": led,
                "bank_dataset_id": bank,
                "scopes": scopes,
                "ledger_debit_means": "outflow",
            },
        )
        assert run_a.status == 201
        rid = run_a.json()["run_id"]

        fake = {
            case: "C-" + new_ulid(),
            ds: "D-" + new_ulid(),
            iid: "IV-" + new_ulid(),
            rid: "RR-0123456789abcdef",
            rep: "RP-" + new_ulid(),
        }
        probes = [
            ("GET", f"/api/cases/{case}", None),
            ("GET", f"/api/datasets/{ds}", None),
            ("GET", f"/api/datasets/{ds}/transactions", None),
            ("GET", f"/api/datasets/{ds}/integrity", None),
            ("GET", f"/api/datasets/{ds}/integrity/issues", None),
            ("GET", f"/api/datasets/{ds}/fixes", None),
            ("GET", f"/api/datasets/{ds}/transformations", None),
            ("GET", f"/api/datasets/{ds}/risk", None),
            ("GET", f"/api/datasets/{ds}/risk/transactions", None),
            ("GET", f"/api/datasets/{ds}/resolution", None),
            ("GET", f"/api/datasets/{ds}/graph", None),
            ("GET", f"/api/datasets/{ds}/pdf", None),
            ("POST", f"/api/datasets/{ds}/forensics", None),
            ("POST", f"/api/datasets/{ds}/anomaly", None),
            ("GET", f"/api/investigations/{iid}", None),
            ("GET", f"/api/investigations/{iid}/notes", None),
            ("GET", f"/api/investigations/{iid}/links", None),
            ("GET", f"/api/investigations/{iid}/activity", None),
            ("GET", f"/api/investigations/{iid}/manifest", None),
            ("GET", f"/api/investigations/{iid}/summary", None),
            ("POST", f"/api/investigations/{iid}/notes", {"body": "from B"}),
            ("GET", f"/api/reconciliation/runs/{rid}", None),
            ("GET", f"/api/reconciliation/runs/{rid}/results", None),
            ("GET", f"/api/reconciliation/runs/{rid}/duplicates", None),
            ("GET", f"/api/reconciliation/runs/{rid}/balances", None),
            ("GET", f"/api/reconciliation/runs/{rid}/findings", None),
            ("GET", f"/api/reports/{rep}", None),
            ("GET", f"/api/reports/{rep}/verify", None),
            ("GET", f"/api/reports/{rep}/download?format=json", None),
        ]

        def masked(text: str, ident: str) -> str:
            return text.replace(ident, "<ID>")

        for method, path, body in probes:
            ident = next(k for k in fake if k in path)
            ra = await b.call(method, path, json_body=body)
            rf = await b.call(method, path.replace(ident, fake[ident]), json_body=body)
            assert ra.status == rf.status and ra.status in (404, 409, 422), (path, ra.status, ra.body[:200])
            assert masked(ra.body.decode(), ident) == masked(rf.body.decode(), fake[ident]), path

        # B's listings and files show nothing of A
        b_sets = {x["dataset_id"] for x in (await b.call("GET", "/api/datasets")).json()["items"]}
        assert ds not in b_sets and {led, bank} <= b_sets  # the seed is B's own copy
        assert rid not in json.dumps((await b.call("GET", "/api/reconciliation/runs")).json())
        a_dir = d.manager.root / wid_of(a)
        b_dir = d.manager.root / wid_of(b)
        assert {p.name for p in (a_dir / "evidence").iterdir()} - {
            p.name for p in (b_dir / "evidence").iterdir()
        }
        assert not (b_dir / "reports").exists() or not any((b_dir / "reports").iterdir())
        assert (await b.call("GET", "/api/audit/verify")).json()["ok"] is True
        b_audit = (await b.call("GET", "/api/audit/verify")).json()["entries"]
        a_audit = (await a.call("GET", "/api/audit/verify")).json()["entries"]
        assert a_audit > b_audit  # A's actions are in A's log only

    run(go())


def test_new_workspaces_get_only_the_synthetic_seed_never_another_visitors_data(make_demo, seed_dir):
    d = make_demo()

    async def go():
        a = Visitor(d.door, client=("198.51.100.30", 1))
        await a.open()
        case = (await a.call("POST", "/api/cases", json_body={"name": "A"})).json()["case_id"]
        assert (await upload(a, case, small_csv(5, "A-only"), "a.csv")).status == 201
        b = Visitor(d.door, client=("198.51.100.31", 1))
        await b.open()
        names = sorted(x["original_filename"] for x in (await b.call("GET", "/api/datasets")).json()["items"])
        assert names == ["SYNTHETIC_bank_statement.csv", "SYNTHETIC_ledger.csv"]
        assert json.loads((seed_dir / "seed.json").read_text())["synthetic"] is True
        assert not [p for p in seed_dir.rglob("*") if "ground_truth" in p.as_posix()]

    run(go())


# ------------------------------------------------------------------------------------------- caches


def test_result_caches_are_namespaced_per_workspace_database(make_demo, seed_dir, tmp_path):
    d = make_demo()
    wa, wb = tmp_path / "wa", tmp_path / "wb"
    shutil.copytree(seed_dir, wa)
    shutil.copytree(seed_dir, wb)
    ca, cb = connect(wa / "ledgerspy.db"), connect(wb / "ledgerspy.db")
    cfa, cfb = d.manager.config_for(wa), d.manager.config_for(wb)
    led = ca.execute("SELECT dataset_id FROM datasets WHERE kind = 'ledger'").fetchone()[0]
    bank = ca.execute("SELECT dataset_id FROM datasets WHERE kind = 'bank_statement'").fetchone()[0]
    assert namespace(ca) == namespace_for_path(wa / "ledgerspy.db") != namespace(cb)
    try:
        for compute in (
            lambda c, cf: risk_service.run_risk(c, cf, led),
            lambda c, cf: resolution_service.run(c, cf, led),
            lambda c, cf: graph_service.run(c, cf, led),
        ):
            ra, rb = compute(ca, cfa), compute(cb, cfb)  # the same dataset ID in two workspaces
            assert ra is not rb and compute(ca, cfa) is ra and compute(cb, cfb) is rb
        forget_namespace(namespace(ca))
        assert risk_service.run_risk(ca, cfa, led) is not None
        assert namespace(cb) in risk_service._CACHE.namespaces()

        su = recon.setup(ca, cfa, led, bank)
        code = {"6964": "LVE", "2401": "LVC"}
        req = recon.RunRequest(
            ledger_dataset_id=led,
            bank_dataset_id=bank,
            ledger_debit_means="outflow",
            scopes=[
                recon.ScopeIn(
                    account_key=x["account_key"],
                    ledger_column="Company Code",
                    ledger_value=code[x["account"][-4:]],
                )
                for x in su["bank"]["accounts"]
            ],
        )
        run_a = recon.start(ca, cfa, req, "t")
        with pytest.raises(LedgerSpyError) as e:
            recon.get_run(cb, cfb, run_a.run_id)  # B does not have A's run: plain not-found ...
        assert e.value.code == "run_not_found" and e.value.http_status == 404
        assert recon.get_run(ca, cfa, run_a.run_id) is run_a  # ... and A's cached run was not touched

        iid = "I-0123456789abcdef"
        inference._CACHE.put(namespace_for_path(wa), iid, {"metadata": {"inference_id": iid, "owner": "A"}})
        with pytest.raises(LedgerSpyError):
            inference.read_metadata(wb, iid)
        assert inference.read_metadata(wa, iid)["owner"] == "A"
    finally:
        ca.close()
        cb.close()
        for c in (risk_service, resolution_service, graph_service, recon, inference):
            c.clear_cache()


def test_namespaced_lru_bounds_each_namespace_separately():
    c = NamespacedLRU(2)
    for i in range(3):
        c.put("a", f"k{i}", i)
    c.put("b", "x", "b-value")
    assert c.get("a", "k0") is None and c.get("a", "k2") == 2 and c.get("b", "x") == "b-value"
    c.put("b", "y", 1)
    c.put("b", "z", 2)  # B's inserts never evict A's entries
    assert c.get("a", "k1") == 1 and c.get("a", "k2") == 2
    c.forget("a")
    assert c.get("a", "k2") is None and c.get("b", "z") == 2


# ------------------------------------------------------------------------------------------- concurrency


class Probe:
    """Stands in for a workspace app: records concurrent calls per workspace and overall."""

    def __init__(self, name: str, stats: dict, delay: float | list[float] = 0.1) -> None:
        self.name, self.stats = name, stats
        self.delays = list(delay) if isinstance(delay, list) else None
        self.delay = delay if not isinstance(delay, list) else 0.0

    async def __call__(self, scope, receive, send):
        delay = self.delays.pop(0) if self.delays else self.delay
        s = self.stats
        s["now"][self.name] = s["now"].get(self.name, 0) + 1
        s["total"] += 1
        s["max"][self.name] = max(s["max"].get(self.name, 0), s["now"][self.name])
        s["max_total"] = max(s["max_total"], s["total"])
        await asyncio.sleep(delay)
        s["now"][self.name] -= 1
        s["total"] -= 1
        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]}
        )
        await send({"type": "http.response.body", "body": b"ok"})


def test_requests_to_one_workspace_are_serialised_and_workspaces_are_independent(make_demo):
    d = make_demo()

    async def go():
        a = Visitor(d.door, client=("198.51.100.40", 1))
        b = Visitor(d.door, client=("198.51.100.41", 1))
        await a.open()
        await b.open()
        stats = {"now": {}, "max": {}, "total": 0, "max_total": 0}
        d.manager._resident[wid_of(a)].app = Probe("a", stats)
        d.manager._resident[wid_of(b)].app = Probe("b", stats)
        calls = [v.call("POST", "/api/cases", json_body={"name": "x"}) for v in (a, b) for _ in range(5)]
        results = await asyncio.gather(*calls)
        assert all(r.status == 200 for r in results)
        assert stats["max"] == {"a": 1, "b": 1}  # never two requests at once inside one workspace
        assert stats["max_total"] == 2  # two workspaces do run at the same time

    run(go())


def test_simultaneous_visits_create_distinct_bounded_workspaces(make_demo):
    d = make_demo(
        max_resident_workspaces=3,
        max_active_workspaces=5,
        new_workspaces_per_hour_per_client=50,
        new_workspaces_per_hour=50,
    )

    async def go():
        visitors = [Visitor(d.door, client=(f"198.51.100.{50 + i}", 1)) for i in range(9)]
        results = await asyncio.gather(*(v.open() for v in visitors))
        ok = [v for v, r in zip(visitors, results, strict=True) if r.status == 200]
        # creation (POST) and the first page view are two steps: a created workspace's page may be briefly
        # refused (503 demo_busy) while every resident slot is in use; it exists and opens on a later visit
        created = [v for v in visitors if v.cookie]
        assert {r.status for r in results} <= {200, 503}
        assert 1 <= len(ok) <= len(created) <= 5
        assert len({wid_of(v) for v in created}) == len(created)  # never a shared workspace
        assert len({v.token for v in ok}) == len(ok)
        assert len(d.manager.workspace_ids_on_disk()) == len(created)
        assert len(d.manager.resident_ids) <= 3
        wid = wid_of(ok[0])
        w1, w2 = await asyncio.gather(d.manager.lease(wid), d.manager.lease(wid))
        assert w1 is w2 and w1.leases == 2  # one manager per workspace, however it is reached
        d.manager.release(w1)
        d.manager.release(w2)

    run(go())


# ------------------------------------------------------------------------------------------- limits


def test_upload_limits_are_enforced_by_the_server(make_demo):
    d = make_demo(max_upload_bytes=4096, max_rows=10, max_uploads_per_workspace=2)

    async def go():
        a = Visitor(d.door)
        await a.open()
        case = (await a.call("POST", "/api/cases", json_body={"name": "x"})).json()["case_id"]
        r = await upload(a, case, small_csv(60))  # > 4096 bytes
        assert r.status == 413 and r.json()["error"]["code"] == "file_too_large"
        r = await a.call("POST", f"/api/cases/{case}/files?filename=x.csv", headers={"content-length": "x"})
        assert r.status == 411
        r = await upload(a, case, small_csv(15, "rows"))  # small enough in bytes, too many rows
        assert r.status in (413, 422) and r.json()["error"]["code"] == "too_many_rows"
        r = await upload(a, case, b"PK\x03\x04" + b"\0" * 100, "archive.zip")
        assert r.status in (415, 422) and r.json()["error"]["code"] in ("unsupported_format", "empty_file")
        assert (await upload(a, case, small_csv(3, "ok"), "ok.csv")).status == 201
        # two uploads were attempted past the front door (rows, zip) but only successful ones count
        assert (await upload(a, case, small_csv(3, "ok2"), "ok2.csv")).status == 201
        r = await upload(a, case, small_csv(3, "third"), "third.csv")
        assert r.status == 413 and r.json()["error"]["code"] == "upload_limit"

    run(go())


def test_pdf_page_limit_is_enforced_by_the_workspace_app(make_demo):
    from tests.backend.test_pdf_datasets import p5_bank_rows, p5_pdf

    d = make_demo(max_pdf_pages=1, max_upload_bytes=2 * 1024 * 1024)
    pdf = p5_pdf(p5_bank_rows()[:60])  # 40 rows per page: two pages

    async def go():
        a = Visitor(d.door)
        await a.open()
        case = (await a.call("POST", "/api/cases", json_body={"name": "x"})).json()["case_id"]
        r = await upload(a, case, pdf, "statement.pdf", kind="bank_statement")
        assert r.status == 413 and r.json()["error"]["code"] == "pdf_too_many_pages"

    run(go())


def test_workspace_quota_and_service_budget(make_demo, seed_dir):
    seed = tree_bytes(seed_dir)
    quota = seed + 64 * 1024
    ui = tree_bytes(REPO / "frontend") + 20_000  # the demo copy of the UI lives in the data folder too
    db = seed_dir / "ledgerspy.db"
    # Stage 2.8: one state-changing request reserves its whole bound (database growth to the quota plus the
    # rollback journal at that size); the budget fits one such request in A, never a second seed copy
    page = allowances.sqlite_page_size(db)
    one_request = 64 * 1024 + allowances.journal_bytes(db.stat().st_size + 64 * 1024, page)
    d = make_demo(
        max_upload_bytes=2048,
        max_workspace_bytes=quota,
        storage_budget_bytes=seed + ui + quota + one_request + 50_000,
    )

    async def go():
        a = Visitor(d.door, client=("198.51.100.60", 1))
        assert (await a.open()).status == 200
        case = (await a.call("POST", "/api/cases", json_body={"name": "x"})).json()["case_id"]
        r = await upload(a, case, small_csv(8))
        while r.status == 201:  # fill the workspace up to its quota
            r = await upload(a, case, small_csv(8, str(time.monotonic())))
        # refused up front (no room for the file), or stopped by the database cap at the quota
        assert r.status in (413, 507) and r.json()["error"]["code"] in (
            "workspace_quota_exceeded",
            "upload_limit",
            "storage_limit_reached",
        )
        assert tree_bytes(d.manager.root / a.cookie.split(".")[0]) <= quota  # never above the quota
        b = Visitor(d.door, client=("198.51.100.61", 1))
        r = await b.open()  # the service budget cannot hold another seed copy plus head-room
        assert r.status == 507 and r.json()["error"]["code"] == "demo_storage_full"

    run(go())


def test_resident_workspaces_are_bounded_and_evicted_safely(make_demo):
    d = make_demo(max_resident_workspaces=1)

    async def go():
        a = Visitor(d.door, client=("198.51.100.70", 1))
        b = Visitor(d.door, client=("198.51.100.71", 1))
        await a.open()
        ns_a = namespace_for_path(d.manager.root / wid_of(a) / "ledgerspy.db")
        led, _ = await seed_ids(a)
        assert (await a.call("POST", f"/api/datasets/{led}/risk")).status == 200
        assert ns_a in risk_service._CACHE.namespaces()
        await b.open()
        assert d.manager.resident_ids == [wid_of(b)]  # A was closed ...
        assert ns_a not in risk_service._CACHE.namespaces()  # ... and its cached results forgotten
        assert (await a.call("GET", "/api/datasets")).status == 200  # reopened (verified) on demand
        assert d.manager.resident_ids == [wid_of(a)]
        ws = await d.manager.lease(wid_of(a))  # A busy: B cannot displace it
        try:
            r = await b.call("GET", "/api/datasets")
            assert r.status == 503 and r.json()["error"]["code"] == "demo_busy"
        finally:
            d.manager.release(ws)
        risk_service.clear_cache()

    run(go())


def test_request_rate_and_processing_time_limits(make_demo):
    d = make_demo(requests_per_minute=3, max_processing_seconds=1)

    async def go():
        a = Visitor(d.door)
        await a.open()
        stats = {"now": {}, "max": {}, "total": 0, "max_total": 0}
        d.manager._resident[wid_of(a)].app = Probe("a", stats, delay=[2.0, 0.0])
        t0 = time.monotonic()
        first, second = await asyncio.gather(
            a.call("POST", "/api/cases", json_body={"name": "slow"}),
            a.call("GET", "/api/datasets"),
        )
        # M3: a state-changing request that timed out may still complete, and the visitor is told so
        assert first.status == 504 and first.json()["error"]["code"] == "still_processing"
        assert (
            second.status == 200 and time.monotonic() - t0 >= 1.9
        )  # waited until the slow call really ended
        assert stats["max"]["a"] == 1
        r = await a.call("GET", "/api/datasets")
        assert r.status == 429 and r.json()["error"]["code"] == "rate_limited"

    run(go())


# ------------------------------------------------------------------------------------ failures and cleanup


def test_a_lost_workspace_is_reported_and_never_silently_replaced(make_demo):
    d = make_demo()

    async def go():
        a = Visitor(d.door, client=("198.51.100.80", 1))
        await a.open()
        wid = wid_of(a)
        d.manager._close(d.manager._resident[wid])
        remove_tree(d.manager.root / wid)  # what a restart of the ephemeral service does
        r = await a.call("GET", "/")
        assert r.status == 409 and r.header("set-cookie") is None
        page = r.body.decode()
        assert "no longer on this server" in page and 'action="/demo/workspace/new"' in page
        assert d.manager.workspace_ids_on_disk() == []  # nothing was created in its place
        r = await a.call("GET", "/api/datasets")
        assert r.status == 409 and r.json()["error"]["code"] == "workspace_restore_failed"
        assert r.json()["error"]["message"] == LOST_REASON
        old_cookie = a.cookie
        r = await a.call("POST", "/demo/workspace/new", headers={"origin": ORIGIN})
        assert r.status == 303 and r.header("location") == "/" and a.cookie != old_cookie
        assert len(d.manager.workspace_ids_on_disk()) == 1
        old = Visitor(d.door, cookie=old_cookie)
        assert (await old.call("GET", "/")).status == 409  # the old ID stays failed until it expires

    run(go())


def test_a_workspace_that_fails_verification_is_kept_until_expiry(make_demo):
    d = make_demo()

    async def go():
        a = Visitor(d.door, client=("198.51.100.81", 1))
        await a.open()
        wid = wid_of(a)
        d.manager._close(d.manager._resident[wid])
        path = d.manager.root / wid
        ev = next((path / "evidence").iterdir())
        os.chmod(ev, stat.S_IWRITE | stat.S_IREAD)
        ev.write_bytes(ev.read_bytes() + b"tampered")
        r = await a.call("GET", "/api/datasets")
        assert r.status == 409 and "evidence" in r.json()["error"]["message"]
        assert path.is_dir()  # kept for inspection, not deleted or replaced
        assert await d.manager.sweep() == []
        d.clock.now += d.manager.retention_seconds + 1
        assert await d.manager.sweep() == [wid]
        assert not path.exists()

    run(go())


def test_cleanup_only_removes_expired_idle_demo_workspaces(make_demo):
    d = make_demo()

    async def go():
        a = Visitor(d.door, client=("198.51.100.90", 1))
        b = Visitor(d.door, client=("198.51.100.91", 1))
        await a.open()
        await b.open()
        decoy = d.manager.root / "notes"
        decoy.mkdir()
        outside = d.data / "keep.txt"
        outside.write_text("keep")
        ws_b = await d.manager.lease(wid_of(b))  # B is in use when everything expires
        d.clock.now += d.manager.retention_seconds + 1
        removed = await d.manager.sweep()
        assert removed == [wid_of(a)]
        assert (d.manager.root / wid_of(b)).is_dir() and decoy.is_dir() and outside.is_file()
        d.manager.release(ws_b)
        assert await d.manager.sweep() == [wid_of(b)]

    run(go())


def test_the_demo_refuses_to_start_outside_demo_mode_and_offline_stays_loopback(tmp_path):
    from ledgerspy.api.app import build_server
    from ledgerspy.config import load_config
    from ledgerspy_demo.limits import DemoConfigError
    from ledgerspy_demo.settings import DemoSettings

    with pytest.raises(DemoConfigError):
        DemoSettings.from_env({"LEDGERSPY_DATA_DIR": str(tmp_path)})
    cfg = load_config()
    assert cfg.settings.server.host == "127.0.0.1"
    import types

    with pytest.raises(ValueError, match="non-loopback"):
        build_server(
            object(),
            types.SimpleNamespace(
                settings=types.SimpleNamespace(server=types.SimpleNamespace(host="0.0.0.0", port=1))
            ),
        )  # noqa: S104
    for path in sorted((REPO / "ledgerspy").rglob("*.py")):
        assert not re.search(r"^\s*(from|import)\s+ledgerspy_demo", path.read_text(encoding="utf-8"), re.M), (
            path
        )
