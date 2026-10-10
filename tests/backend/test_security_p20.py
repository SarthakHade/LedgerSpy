"""P20 security hardening: regression tests for the issues found in the audit, and checks of the controls the
threat model relies on (docs/p20-security-acceptance.md). Synthetic or hand-made data only."""

import json
import random
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ledgerspy.api.app import create_app
from ledgerspy.api.limits import MAX_BODY_BYTES, PAGE_MAX
from ledgerspy.api.security import build_csp, inline_markup
from ledgerspy.cases import audit
from ledgerspy.cases.db import check_triggers, connect, expected_triggers
from ledgerspy.errors import LedgerSpyError
from ledgerspy.paths import FRONTEND_DIR
from ledgerspy.reports import render
from tests.backend.conftest import BASE_URL, FIXTURES, make_ingest
from tests.backend.xlsx_builder import build_xlsx

REPO = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------------------------------- authentication


def _api_routes(app) -> list[tuple[str, str]]:
    """Every registered API operation (from the app's own OpenAPI document), with dummy path parameters."""
    out = [("GET", "/api/openapi.json")]
    for path, ops in app.openapi()["paths"].items():
        if path.startswith("/api"):
            out += [(m.upper(), re.sub(r"\{[^}]+\}", "X", path)) for m in sorted(ops)]
    return out


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer wrong-token-wrong-token-wrong-token-wrong"},
        {"Authorization": "Basic Zm9vOmJhcg=="},
        {"Authorization": "Token abc"},
    ],
)
def test_every_api_route_requires_the_token(client, headers):
    routes = _api_routes(client.app)
    assert len(routes) >= 80  # every router is registered (cases … reports)
    for method, path in routes:
        r = client.request(method, path, headers=headers)
        if path == "/api/health":
            assert r.status_code == 200
            continue
        assert r.status_code == 401, (method, path, r.status_code)
        assert r.json() == {"detail": "session token required"}


def test_token_only_from_the_authorization_header(client, token):
    assert client.get("/api/cases", params={"token": token}).status_code == 401
    client.cookies.set("ledgerspy_session", token)  # the page cookie never authorises an API call
    assert client.get("/api/datasets").status_code == 401
    client.cookies.clear()
    assert client.get("/api/datasets", headers={"X-Session-Token": token}).status_code == 401


def test_host_origin_and_fetch_metadata_checks(client, auth):
    for host in ("", "evil.example", "127.0.0.1", "127.0.0.1:1", "[::1]:8765", "0.0.0.0:8765"):
        assert client.get("/api/datasets", headers=auth | {"Host": host}).status_code == 400, host
    for origin in ("null", "http://evil.example", "http://127.0.0.1:1", "https://127.0.0.1:8765"):
        assert client.get("/api/datasets", headers=auth | {"Origin": origin}).status_code == 403, origin
    assert client.get("/api/datasets", headers=auth | {"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/datasets", headers=auth | {"Origin": BASE_URL}).status_code == 200


# ------------------------------------------------------------------------------------------- pagination (fix)


def test_huge_page_numbers_are_refused_not_server_errors(client, auth):
    """Before P20 `page=10**20` overflowed SQLite's OFFSET and returned HTTP 500."""
    paged = [
        path
        for path, ops in client.app.openapi()["paths"].items()
        for m, op in ops.items()
        if m == "get" and any(p["name"] == "page" for p in op.get("parameters", []))
    ]
    assert len(paged) >= 20
    for path in paged:  # every paginated route refuses it before reaching a service
        r = client.get(re.sub(r"\{[^}]+\}", "X", path), params={"page": 10**20}, headers=auth)
        assert r.status_code == 422, (path, r.status_code)
    for path in ["/api/investigations", "/api/reports"]:
        assert client.get(path, params={"page": PAGE_MAX}, headers=auth).json()["items"] == []
        assert client.get(path, params={"page": PAGE_MAX + 1}, headers=auth).status_code == 422


# ------------------------------------------------------------------------------------------- body size (fix)


def test_oversized_bodies_are_refused_before_parsing(client, auth):
    big = json.dumps({"name": "x" * (MAX_BODY_BYTES + 1)})
    r = client.post("/api/cases", content=big, headers=auth | {"Content-Type": "application/json"})
    assert r.status_code == 413 and "larger than" in r.json()["detail"]

    def chunks():  # no Content-Length: counted as it streams
        for _ in range(MAX_BODY_BYTES // 65536 + 2):
            yield b"x" * 65536

    r = client.post("/api/cases", content=chunks(), headers=auth | {"Content-Type": "application/json"})
    assert r.status_code == 413
    r = client.post("/api/cases", content=b"{}", headers=auth | {"Content-Length": "12x"})
    assert r.status_code == 400
    assert client.post("/api/cases", json={"name": "small"}, headers=auth).status_code == 201


def test_upload_keeps_its_own_streaming_limit(tmp_path, db_path, token):
    from ledgerspy.config import load_config

    cfg = load_config(
        overrides={"workspace": {"path": str(db_path.parent)}, "ingest": {"max_file_bytes": 2048}}
    )
    with TestClient(create_app(cfg, db_path, token), base_url=BASE_URL) as c:
        auth = {"Authorization": f"Bearer {token}"}
        case = c.post("/api/cases", json={"name": "u"}, headers=auth).json()["case_id"]
        r = c.post(
            f"/api/cases/{case}/files", params={"filename": "a.csv"}, content=b"a,b\n" * 1000, headers=auth
        )
        assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"


# ------------------------------------------------------------------------------------------- file names (fix)


@pytest.mark.parametrize(
    "name",
    [
        "inv\u202ecsv.exe",
        "a\x00.csv",
        "a\nb.csv",
        "a\x85.csv",
        "a\u2028.csv",
        "\t",
        "   ",
        "x" * 256,
        "\u2067x.csv",
    ],
)
def test_unsafe_original_file_names_are_refused_and_nothing_is_stored(ingest, name):
    before = ingest.conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
    with pytest.raises(LedgerSpyError) as e:
        ingest.upload(FIXTURES / "ledger_normal.csv", name)
    assert e.value.code == "invalid_filename"
    assert ingest.conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == before
    assert ingest.conn.execute("SELECT COUNT(*) FROM evidence_files").fetchone()[0] == 0


def test_path_like_names_stay_metadata(ingest):
    """Separators and dots are not refused: the name is display metadata only (the P3 contract)."""
    ds = ingest.upload(FIXTURES / "ledger_normal.csv", "../reports/a.csv")
    assert ds["evidence"]["original_filename"] == "../reports/a.csv"
    assert not (ingest.workspace.parent / "reports").exists()


def test_ordinary_unicode_file_names_are_kept(ingest):
    name = "लेजर \u200d 2026 (copy).csv"  # Devanagari with a zero-width joiner
    ds = ingest.upload(FIXTURES / "ledger_normal.csv", name)
    assert ds["evidence"]["original_filename"] == name


def test_the_api_reports_an_unsafe_file_name(client, auth):
    case = client.post("/api/cases", json={"name": "n"}, headers=auth).json()["case_id"]
    r = client.post(
        f"/api/cases/{case}/files",
        params={"filename": "pay\u202eslx.csv"},
        content=(FIXTURES / "ledger_normal.csv").read_bytes(),
        headers=auth,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_filename"


def test_no_invisible_direction_characters_in_the_source_tree():
    """Trojan-source guard: bidi controls in code or the UI could make reviewed text differ from what runs."""
    bad = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
    hits = []
    for root in ("ledgerspy", "frontend", "tests", "config", "docs"):
        for p in (REPO / root).rglob("*"):
            source = p.suffix in (".py", ".js", ".html", ".css", ".yaml", ".md") and "fixtures" not in p.parts
            if source and bad.search(p.read_text(encoding="utf-8", errors="replace")):
                hits.append(str(p.relative_to(REPO)))
    assert hits == []


# ------------------------------------------------------------------------------------------- CSP (roadmap)


def test_served_page_has_a_strict_csp_and_no_inline_markup(client, token):
    client.get(f"/?token={token}", follow_redirects=False)
    r = client.get("/")
    assert r.status_code == 200 and inline_markup(r.text) == []
    csp = r.headers["content-security-policy"]
    assert "unsafe-inline" not in csp and "unsafe-hashes" not in csp and "unsafe-eval" not in csp
    assert "script-src 'self'" in csp and "style-src 'self'" in csp


def test_a_page_with_inline_markup_is_refused_at_startup(tmp_path, config, db_path, token):
    for bad in (
        '<button onclick="x()">',
        '<p style="color:red">',
        "<script>alert(1)</script>",
        "<style>p{}</style>",
    ):
        front = tmp_path / f"front{abs(hash(bad))}"
        shutil.copytree(FRONTEND_DIR, front)
        page = (front / "index.html").read_text(encoding="utf-8").replace("</body>", bad + "</body>")
        (front / "index.html").write_text(page, encoding="utf-8")
        with pytest.raises(ValueError, match="strict CSP"):
            create_app(config, db_path, token, frontend_dir=front)
    with pytest.raises(ValueError):
        build_csp('<a onmouseover="x">')


def test_report_html_allows_only_its_own_stylesheet():
    assert "unsafe-inline" not in render.CSP and "style-src 'sha256-" in render.CSP
    assert "script-src" not in render.CSP and render.CSP.startswith("default-src 'none'")


# ------------------------------------------------------------------------------------------- static files


def test_static_files_never_leave_the_frontend_folder(client, db_path):
    secret = db_path.read_bytes()[:16]
    for p in (
        "/../ledgerspy.db",
        "/%2e%2e/ledgerspy.db",
        "/js/..%2f..%2fpyproject.toml",
        "/css/../../pyproject.toml",
        "/ledgerspy.db",
        "/..%5cpyproject.toml",
        "/%252e%252e/pyproject.toml",
    ):
        r = client.get(p)
        assert r.status_code == 404, p
        assert secret not in r.content and b"[project]" not in r.content


# ------------------------------------------------------------------------------------------- audit (fix)


def _copy_db(db_path: Path, tmp_path: Path) -> Path:
    dst = tmp_path / "copy.db"
    src = sqlite3.connect(db_path)
    out = sqlite3.connect(dst)
    src.backup(out)
    src.close()
    out.close()
    return dst


def test_trigger_set_matches_the_migrations(client, auth, db_path):
    client.post("/api/cases", json={"name": "t"}, headers=auth)
    v = client.get("/api/audit/verify", headers=auth).json()
    assert (
        v["ok"]
        and v["chain_ok"]
        and v["triggers"]["ok"]
        and v["triggers"]["expected"] == len(expected_triggers())
    )
    assert v["triggers"]["expected"] >= 50 and v["head_seq"] == v["entries"]


def test_dropped_altered_or_extra_triggers_are_detected(client, auth, db_path, tmp_path):
    client.post("/api/cases", json={"name": "t"}, headers=auth)
    copy = _copy_db(db_path, tmp_path)
    c = sqlite3.connect(copy)
    c.execute("DROP TRIGGER investigation_notes_no_update")  # a P15 table: missed by the pre-P20 verify
    c.execute("DROP TRIGGER reports_no_delete")
    c.execute("CREATE TRIGGER reports_no_delete BEFORE DELETE ON reports BEGIN SELECT 1; END")  # neutered
    c.execute("CREATE TRIGGER sneaky AFTER INSERT ON cases BEGIN SELECT 1; END")
    c.commit()
    r = check_triggers(c)
    assert r == {
        "ok": False,
        "expected": len(expected_triggers()),
        "missing": ["investigation_notes_no_update"],
        "altered": ["reports_no_delete"],
        "unexpected": ["sneaky"],
    }
    assert audit.verify(c).ok  # the hash chain alone cannot see it — hence the separate check
    c.close()
    out = subprocess.run(
        [sys.executable, "-m", "ledgerspy", "audit", "verify", "--db", str(copy)],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=120,
        check=False,
    )
    assert out.returncode == 1 and "triggers differ" in out.stdout


def test_api_verify_fails_when_a_trigger_is_dropped(config, db_path, token, tmp_path):
    with TestClient(create_app(config, db_path, token), base_url=BASE_URL) as c:
        c.post("/api/cases", json={"name": "t"}, headers={"Authorization": f"Bearer {token}"})
    raw = sqlite3.connect(db_path)
    raw.execute("DROP TRIGGER ocr_corrections_no_delete")
    raw.commit()
    raw.close()
    with TestClient(create_app(config, db_path, token), base_url=BASE_URL) as c:
        v = c.get("/api/audit/verify", headers={"Authorization": f"Bearer {token}"}).json()
    assert v["chain_ok"] and not v["ok"] and v["triggers"]["missing"] == ["ocr_corrections_no_delete"]


def _tamper(path: Path, sql: list[str]) -> None:
    """Edit the database as an administrator could: drop the audit triggers, change, restore them exactly."""
    c = sqlite3.connect(path)
    c.execute("DROP TRIGGER audit_log_no_update")
    c.execute("DROP TRIGGER audit_log_no_delete")
    for s in sql:
        c.execute(s)
    c.execute(audit._NO_UPDATE)
    c.execute(audit._NO_DELETE)
    c.commit()
    c.close()


def test_truncation_is_invisible_without_an_anchor_and_caught_with_one(client, auth, db_path, tmp_path):
    for i in range(4):
        client.post("/api/cases", json={"name": f"c{i}"}, headers=auth)
    v = client.get("/api/audit/verify", headers=auth).json()
    anchor = {"anchor_seq": v["head_seq"], "anchor_hash": v["head_hash"]}
    assert client.get("/api/audit/verify", params=anchor, headers=auth).json()["anchor"]["matches"]
    copy = _copy_db(db_path, tmp_path)
    _tamper(copy, [f"DELETE FROM audit_log WHERE seq > {v['head_seq'] - 2}"])  # drop the two newest entries
    c = connect(copy)
    assert audit.verify(c).ok and check_triggers(c)["ok"]  # documented limitation: undetectable from the DB
    c.close()
    shutil.copy(copy, db_path)
    after = client.get("/api/audit/verify", params=anchor, headers=auth).json()
    assert after["chain_ok"] and not after["ok"]
    assert after["anchor"] == {
        "seq": anchor["anchor_seq"],
        "matches": False,
        "reason": "the anchored entry is missing (the log was truncated below it)",
    }
    assert client.get("/api/audit/verify", params={"anchor_seq": 1}, headers=auth).status_code == 422


def test_edit_of_an_audit_entry_is_detected_and_anchor_catches_a_rewrite(client, auth, db_path, tmp_path):
    for i in range(3):
        client.post("/api/cases", json={"name": f"c{i}"}, headers=auth)
    v = client.get("/api/audit/verify", headers=auth).json()
    copy = _copy_db(db_path, tmp_path)
    _tamper(copy, ["UPDATE audit_log SET actor = 'someone-else' WHERE seq = 2"])
    c = connect(copy)
    r = audit.verify(c)
    assert not r.ok and r.failed_seq == 2
    c.close()
    # rewrite the whole chain consistently (possible for whoever controls the file and the code)
    c = sqlite3.connect(copy)
    rows = c.execute("SELECT seq, ts, actor, action, object, details FROM audit_log ORDER BY seq").fetchall()
    prev, new = audit.GENESIS_HASH, []
    for seq, ts, actor, action, obj, details in rows:
        h = audit.entry_hash(prev, seq, ts, actor, action, obj, details)
        new.append((prev, h, seq))
        prev = h
    c.close()
    _tamper(
        copy,
        ["DELETE FROM audit_log"]
        + [
            f"INSERT INTO audit_log VALUES ({seq}, '{ts}', '{actor}', '{action}', '{obj}', '{details}', "
            f"'{p}', '{h}')"
            for (p, h, seq), (_s, ts, actor, action, obj, details) in zip(new, rows, strict=True)
        ],
    )
    c = connect(copy)
    assert audit.verify(c).ok  # a consistent rewrite passes the chain check …
    c.close()
    shutil.copy(copy, db_path)
    after = client.get(
        "/api/audit/verify", params={"anchor_seq": v["head_seq"], "anchor_hash": v["head_hash"]}, headers=auth
    ).json()
    assert not after["ok"] and after["anchor"]["reason"].startswith(
        "the anchored entry's hash differs"
    )  # … not an anchor


def test_edits_while_a_record_trigger_is_dropped_and_restored_are_not_detected(client, auth, db_path):
    """Documented limitation: the append-only triggers stop the application and casual edits, but whoever
    controls the file can drop a trigger, edit a record and restore the trigger exactly. Neither the hash
    chain nor the trigger check can reveal that (the evidence bytes themselves are still re-hashed on use)."""
    case = client.post("/api/cases", json={"name": "lim"}, headers=auth).json()["case_id"]
    client.post(
        f"/api/cases/{case}/files",
        params={"filename": "l.csv"},
        content=(FIXTURES / "ledger_normal.csv").read_bytes(),
        headers=auth,
    )
    c = sqlite3.connect(db_path)
    trig = c.execute("SELECT sql FROM sqlite_master WHERE name = 'evidence_files_no_update'").fetchone()[0]
    c.execute("DROP TRIGGER evidence_files_no_update")
    c.execute("UPDATE evidence_files SET stored_at = '1999-01-01T00:00:00Z'")
    c.execute(trig)
    c.commit()
    c.close()
    v = client.get("/api/audit/verify", headers=auth).json()
    assert v["ok"] and v["triggers"]["ok"]  # not detected — stated in the limitations, not claimed otherwise


def test_audit_entries_never_contain_the_session_token(client, auth, token, db_path):
    case = client.post("/api/cases", json={"name": "secret-check"}, headers=auth).json()["case_id"]
    client.post(
        f"/api/cases/{case}/files",
        params={"filename": "l.csv"},
        content=(FIXTURES / "ledger_normal.csv").read_bytes(),
        headers=auth,
    )
    c = connect(db_path)
    dump = "\n".join(str(tuple(r)) for r in c.execute("SELECT * FROM audit_log"))
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    everything = dump + "".join(
        str(c.execute(f"SELECT * FROM {t}").fetchall())
        for t in tables  # noqa: S608 — names from the schema
    )
    c.close()
    assert token not in everything


# ------------------------------------------------------------------------------------------- errors


def test_error_responses_do_not_reveal_paths_or_internals(client, auth, db_path):
    ws = str(db_path.parent)
    probes = [
        client.get("/api/datasets/D-01J00000000000000000000000", headers=auth),
        client.get("/api/reports/RP-01J00000000000000000000000", headers=auth),
        client.post("/api/cases", content=b"{bad", headers=auth | {"Content-Type": "application/json"}),
        client.get("/api/investigations/IV-%00", headers=auth),
        client.get("/nope"),
    ]
    for r in probes:
        body = r.text
        assert ws not in body and ws.replace("\\", "\\\\") not in body and "Traceback" not in body, body[:200]


# ------------------------------------------------------------------------------------------- hostile inputs


def _mutations(data: bytes, n: int, seed: int):
    rnd = random.Random(seed)
    for _ in range(n):
        b = bytearray(data)
        for _ in range(rnd.randint(1, 8)):
            op = rnd.random()
            i = rnd.randrange(len(b)) if b else 0
            if op < 0.4 and b:
                b[i] = rnd.randrange(256)
            elif op < 0.6:
                b[i:i] = bytes(rnd.randrange(256) for _ in range(rnd.randint(1, 16)))
            elif op < 0.8 and b:
                del b[i : i + rnd.randint(1, 64)]
            else:
                b = b[: rnd.randrange(len(b) + 1)]
        yield bytes(b)


def test_mutated_csv_and_xlsx_never_crash_the_importer(ingest):
    """Deterministic mutation fuzz: every malformed file is either imported or refused with an explicit
    error; nothing else escapes, and no refused file leaves a dataset behind."""
    csv_seed = (FIXTURES / "ledger_normal.csv").read_bytes()
    xlsx_seed = build_xlsx(
        {"Sheet1": [["Date", "Amount", "Vendor"], ["2024-01-01", 10.5, "A"], ["2024-01-02", 20, "B"]]}
    )
    outcomes = {"imported": 0, "refused": 0}
    for name, seed, n in (("f.csv", csv_seed, 60), ("f.xlsx", xlsx_seed, 60)):
        for data in _mutations(seed, n, seed=len(name)):
            before = ingest.conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
            try:
                ingest.upload(data, name)
                outcomes["imported"] += 1
            except LedgerSpyError as e:
                assert e.code and e.http_status in (409, 413, 415, 422), (e.code, e.http_status)
                assert ingest.conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == before
                outcomes["refused"] += 1
    assert outcomes["refused"] > 0 and sum(outcomes.values()) == 120


def test_mutated_pdfs_never_crash_the_importer(tmp_path):
    from tests.backend.test_pdf_datasets import p5_bank_rows, p5_pdf

    h = make_ingest(tmp_path)
    seed = p5_pdf(p5_bank_rows()[:8])
    for data in _mutations(seed, 12, seed=7):
        try:
            h.upload(data, "s.pdf", kind="bank_statement")
        except LedgerSpyError as e:
            assert e.code and e.http_status in (409, 413, 415, 422), (e.code, e.http_status)
    h.conn.close()
