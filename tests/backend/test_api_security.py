import re

import pytest

from ledgerspy.api.app import TOKEN_META
from ledgerspy.api.security import SESSION_COOKIE, inline_handler_hashes
from ledgerspy.cases import audit
from ledgerspy.cases.db import connect
from ledgerspy.paths import FRONTEND_DIR
from tests.backend.conftest import BASE_URL

REQUIRED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "cache-control": "no-store",
}


def login(client, token):
    r = client.get(f"/?token={token}", follow_redirects=False)
    assert r.status_code == 303
    return r


# ---------- health ----------


def test_health_is_public(client, config):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["schema_version"] == 1
    assert body["config_sha256"] == config.sha256


# ---------- token requirement ----------


def test_case_endpoints_reject_requests_without_token(client):
    r = client.post("/api/cases", json={"name": "Acme FY26"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert client.get("/api/cases/C-01J00000000000000000000000").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer wrong-token-wrong-token-wrong-token-wrong"},
        {"Authorization": "Basic Zm9vOmJhcg=="},
        {"Authorization": "Bearer"},
        {"X-LedgerSpy-Token": "anything"},
    ],
)
def test_wrong_credentials_are_rejected(client, headers):
    assert client.post("/api/cases", json={"name": "x"}, headers=headers).status_code == 401


def test_session_cookie_alone_does_not_authorise_api_calls(client, token):
    login(client, token)
    assert client.cookies.get(SESSION_COOKIE)
    assert client.post("/api/cases", json={"name": "x"}).status_code == 401


def test_token_in_query_string_does_not_authorise_api_calls(client, token):
    assert client.post(f"/api/cases?token={token}", json={"name": "x"}).status_code == 401


def test_unknown_api_paths_and_openapi_are_protected(client, auth):
    assert client.get("/api/does-not-exist").status_code == 401
    assert client.get("/api/openapi.json").status_code == 401
    assert client.get("/api/openapi.json", headers=auth).status_code == 200
    assert client.get("/docs").status_code == 404  # Swagger UI would load assets from a CDN


# ---------- case skeleton ----------


def test_create_and_get_case_with_token(client, auth, db_path):
    r = client.post(
        "/api/cases",
        json={
            "name": " Acme FY26 ",
            "client": "Acme",
            "period_start": "2025-04-01",
            "period_end": "2026-03-31",
        },
        headers=auth,
    )
    assert r.status_code == 201
    case = r.json()
    assert re.fullmatch(r"C-[0-9A-HJKMNP-TV-Z]{26}", case["case_id"])
    assert case["name"] == "Acme FY26" and case["status"] == "open"
    assert client.get(f"/api/cases/{case['case_id']}", headers=auth).json() == case

    conn = connect(db_path, read_only=True)
    result = audit.verify(conn)
    last = conn.execute("SELECT action, object FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    conn.close()
    assert result.ok and result.entries == 2
    assert tuple(last) == ("case.created", f"case:{case['case_id']}")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "x", "status": "closed"},
        {"name": "x", "period_start": "2026-04-01", "period_end": "2026-03-31"},
    ],
)
def test_invalid_case_payloads_are_rejected(client, auth, body):
    assert client.post("/api/cases", json=body, headers=auth).status_code == 422


def test_case_lookup_errors(client, auth):
    assert client.get("/api/cases/C-01J00000000000000000000000", headers=auth).status_code == 404
    assert client.get("/api/cases/not-an-id", headers=auth).status_code == 422


# ---------- Host / Origin ----------


@pytest.mark.parametrize(
    "host", ["evil.example", "127.0.0.1:9999", "192.168.1.10:8765", "attacker.test:8765"]
)
def test_foreign_host_header_is_rejected_everywhere(client, host):
    for path in ("/api/health", "/", "/js/shared.js"):
        assert client.get(path, headers={"Host": host}).status_code == 400


def test_localhost_alias_is_accepted(client):
    assert client.get("/api/health", headers={"Host": "localhost:8765"}).status_code == 200


@pytest.mark.parametrize(
    "origin", ["http://evil.example", "null", "http://127.0.0.1:9999", "https://127.0.0.1:8765"]
)
def test_foreign_origin_is_rejected(client, auth, origin):
    assert (
        client.post("/api/cases", json={"name": "x"}, headers={**auth, "Origin": origin}).status_code == 403
    )


def test_same_origin_request_is_accepted(client, auth):
    r = client.post("/api/cases", json={"name": "x"}, headers={**auth, "Origin": BASE_URL})
    assert r.status_code == 201


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_site_fetch_metadata_is_rejected(client, auth, site):
    assert client.get("/api/health", headers={"Sec-Fetch-Site": site}).status_code == 403


# ---------- security headers ----------


def test_security_headers_on_every_kind_of_response(client, token, auth):
    login(client, token)
    responses = [
        client.get("/"),  # UI page
        client.get("/js/shared.js"),  # static asset
        client.get("/api/health"),  # public API
        client.post("/api/cases", json={"name": "x"}, headers=auth),  # protected API
        client.post("/api/cases", json={"name": "x"}),  # 401
        client.get("/nope.txt"),  # 404
        client.get("/api/health", headers={"Host": "evil"}),  # 400
    ]
    for r in responses:
        for name, value in REQUIRED_HEADERS.items():
            assert r.headers.get(name) == value, (r.request.url, name)
        assert "content-security-policy" in r.headers


def test_csp_is_strict_for_scripts(client):
    csp = dict(
        part.strip().split(" ", 1)
        for part in client.get("/api/health").headers["content-security-policy"].split(";")
    )
    assert "'unsafe-inline'" not in csp["script-src"] and "'unsafe-eval'" not in csp["script-src"]
    assert csp["default-src"] == "'self'"
    assert (
        csp["object-src"] == "'none'" and csp["frame-ancestors"] == "'none'" and csp["base-uri"] == "'none'"
    )
    page = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    hashes = inline_handler_hashes(page)
    assert len(hashes) >= 20  # the existing UI's onclick= handlers, allowed one by one
    assert all(h in csp["script-src"] for h in hashes)


def test_inline_handler_hash_is_computed_on_the_decoded_attribute():
    # Browser hashes the attribute value after entity decoding.
    import base64
    import hashlib

    expected = base64.b64encode(hashlib.sha256(b"go('a')").digest()).decode()
    assert inline_handler_hashes('<b onclick="go(&#39;a&#39;)">') == [f"'sha256-{expected}'"]


# ---------- serving the existing UI ----------


def test_ui_is_locked_without_the_entry_token(client):
    r = client.get("/")
    assert r.status_code == 401
    assert "ledgerspy serve" in r.text
    assert client.get("/?token=wrong", follow_redirects=False).status_code == 401


def test_entry_token_sets_strict_httponly_cookie_and_serves_ui(client, token):
    r = login(client, token)
    assert r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    page = client.get("/")
    assert page.status_code == 200
    assert "LedgerSpy" in page.text and 'id="file-input"' in page.text
    assert f'<meta name="{TOKEN_META}" content="{token}">' in page.text


def test_served_ui_is_the_unmodified_frontend_plus_token_meta(client, token):
    login(client, token)
    original = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    served = client.get("/").text
    assert served.replace(f'\n<meta name="{TOKEN_META}" content="{token}">', "", 1) == original
    for asset in (
        "css/main.css",
        "css/chatbot.css",
        "js/shared.js",
        "js/data-pipeline.js",
        "js/ui-core.js",
        "js/ml-engine.js",
        "js/chatbot-ui.js",
    ):
        r = client.get(f"/{asset}")
        assert r.status_code == 200 and r.content == (FRONTEND_DIR / asset).read_bytes(), asset


def test_index_html_alias_goes_through_the_gate(client):
    r = client.get("/index.html", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/"


def test_static_path_traversal_is_refused(client):
    assert client.get("/../pyproject.toml").status_code in (400, 404)
    assert client.get("/%2e%2e/pyproject.toml").status_code in (400, 404)
