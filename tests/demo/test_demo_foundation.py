"""Stage 1 of the hosted public demo (docs/demo/public-demo.md): limits, settings, workspace identity, and the
separation from the offline application.

Run explicitly: `python -m pytest tests/demo` (the offline release suite, tests/backend, is unchanged).
"""

import ast
import sys
from pathlib import Path

import pytest

from ledgerspy.config import load_config
from ledgerspy_demo.identity import (
    is_workspace_id,
    manifest_object_key,
    new_workspace_id,
    read_cookie,
    sign_cookie,
    snapshot_object_key,
    workspace_dir,
)
from ledgerspy_demo.limits import DemoConfigError, DemoLimits
from ledgerspy_demo.settings import DemoSettings

REPO = Path(__file__).resolve().parents[2]
KEY = b"k" * 16 + b"0123456789abcdef"  # test-only value, 32 bytes
WEEK = 7 * 24 * 3600


def demo_env(**extra: str) -> dict[str, str]:
    env = {
        "LEDGERSPY_MODE": "demo",
        "LEDGERSPY_DEMO_SESSION_KEY": "test-only-0123456789abcdefghijklmnopqrstuv",
        "LEDGERSPY_DEMO_DATA_DIR": str(REPO / "does-not-need-to-exist"),
        "LEDGERSPY_DEMO_PUBLIC_ORIGIN": "https://ledgerspy-demo.example.org",
    }
    env.update(extra)
    return env


# ------------------------------------------------------------------------------------------- limits


def test_default_limits_are_stricter_than_the_offline_configuration():
    limits = DemoLimits()
    settings = load_config().settings
    limits.check_against_offline(settings)  # raises if any demo limit is looser
    assert limits.max_upload_bytes < settings.ingest.max_file_bytes
    assert limits.max_rows < settings.ingest.max_rows
    assert limits.max_pdf_pages < settings.pdf.max_pages
    assert limits.retention_days == 7


def test_a_demo_limit_looser_than_offline_is_refused():
    with pytest.raises(DemoConfigError, match="exceeds the offline limit"):
        DemoLimits(max_rows=2_000_000).check_against_offline(load_config().settings)


@pytest.mark.parametrize(
    "env, message",
    [
        ({"LEDGERSPY_DEMO_MAX_ROWS": "-5"}, "positive integer"),
        ({"LEDGERSPY_DEMO_MAX_ROWS": "1e6"}, "positive integer"),
        ({"LEDGERSPY_DEMO_MAX_ROWS": "0"}, "positive integer"),
        ({"LEDGERSPY_DEMO_RETENTION_DAYS": "30"}, "must not exceed 7"),
        ({"LEDGERSPY_DEMO_MAX_WORKSPACE_BYTES": "1"}, "at least max_upload_bytes"),
    ],
)
def test_invalid_limit_overrides_are_refused_not_clamped(env, message):
    with pytest.raises(DemoConfigError, match=message):
        DemoLimits.from_env(env)


def test_limit_overrides_can_be_stricter():
    limits = DemoLimits.from_env({"LEDGERSPY_DEMO_MAX_PDF_PAGES": "2", "LEDGERSPY_DEMO_RETENTION_DAYS": "3"})
    assert (limits.max_pdf_pages, limits.retention_days) == (2, 3)


# ------------------------------------------------------------------------------------------- settings


def test_demo_settings_refuse_to_load_outside_demo_mode():
    for env in ({}, {"LEDGERSPY_MODE": "offline"}, {"LEDGERSPY_MODE": "DEMO "}):
        with pytest.raises(DemoConfigError, match="not enabled"):
            DemoSettings.from_env({**demo_env(), **env} if env else {})


@pytest.mark.parametrize(
    "override, message",
    [
        ({"LEDGERSPY_DEMO_SESSION_KEY": ""}, "required"),
        ({"LEDGERSPY_DEMO_SESSION_KEY": "short"}, "at least 32"),
        ({"LEDGERSPY_DEMO_SESSION_KEY": "a" * 40}, "random"),
        ({"LEDGERSPY_DEMO_DATA_DIR": "relative/dir"}, "absolute"),
        ({"LEDGERSPY_DEMO_PUBLIC_ORIGIN": "http://demo.example.org"}, "https"),
        ({"LEDGERSPY_DEMO_PUBLIC_ORIGIN": "https://demo.example.org/app"}, "bare origin"),
        ({"LEDGERSPY_DEMO_STORAGE_URL": "https://x.supabase.co"}, "set all of"),
    ],
)
def test_demo_settings_validation(override, message):
    with pytest.raises(DemoConfigError, match=message):
        DemoSettings.from_env(demo_env(**override))


def test_demo_settings_without_storage_are_explicitly_ephemeral():
    s = DemoSettings.from_env(demo_env(LEDGERSPY_DEMO_PUBLIC_ORIGIN="https://Demo.Example.org/"))
    assert s.public_origin == "https://demo.example.org" and s.persistent is False


def test_secrets_never_appear_in_the_settings_repr():
    s = DemoSettings.from_env(
        demo_env(
            LEDGERSPY_DEMO_STORAGE_URL="https://project.supabase.co",
            LEDGERSPY_DEMO_STORAGE_BUCKET="ledgerspy-demo-private",
            LEDGERSPY_DEMO_STORAGE_SERVICE_KEY="service-key-test-only-value",
        )
    )
    text = repr(s)
    assert s.persistent is True
    assert "test-only-0123456789" not in text and "service-key-test-only-value" not in text
    assert "<redacted>" in text


def test_loopback_http_is_allowed_only_for_local_testing():
    s = DemoSettings.from_env(demo_env(LEDGERSPY_DEMO_PUBLIC_ORIGIN="http://127.0.0.1:8765"))
    assert s.public_origin == "http://127.0.0.1:8765"


# ------------------------------------------------------------------------------------------- identity


def test_workspace_ids_are_random_and_strict():
    ids = {new_workspace_id() for _ in range(1000)}
    assert len(ids) == 1000 and all(is_workspace_id(i) for i in ids)
    for bad in ("W-" + "A" * 32, "W-" + "0" * 31, "../W-" + "0" * 32, "W-" + "0" * 32 + "/x", None, 5):
        assert not is_workspace_id(bad)


def test_signed_cookie_round_trip_and_rejections():
    wid = new_workspace_id()
    cookie = sign_cookie(KEY, wid, 1_000_000)
    assert read_cookie(KEY, cookie, 1_000_100, WEEK) == wid
    other = new_workspace_id()
    head, issued, mac = cookie.split(".")
    for forged in (
        f"{other}.{issued}.{mac}",  # another workspace with this MAC
        f"{head}.{int(issued) + 1}.{mac}",  # extended issue time
        f"{head}.{issued}.{'0' * 64}",
        cookie.upper(),
        cookie + "x",
        "",
        None,
    ):
        assert read_cookie(KEY, forged, 1_000_100, WEEK) is None
    assert read_cookie(b"x" * 32, cookie, 1_000_100, WEEK) is None  # wrong key
    assert read_cookie(KEY, cookie, 1_000_000 + WEEK + 1, WEEK) is None  # expired
    assert read_cookie(KEY, sign_cookie(KEY, wid, 2_000_000), 1_000_000, WEEK) is None  # from the future


def test_signing_refuses_client_style_ids_and_short_keys():
    with pytest.raises(ValueError):
        sign_cookie(KEY, "../../etc", 1)
    with pytest.raises(ValueError):
        sign_cookie(b"short", new_workspace_id(), 1)


def test_workspace_paths_and_object_keys_come_only_from_validated_ids(tmp_path):
    wid = new_workspace_id()
    assert workspace_dir(tmp_path, wid) == (tmp_path / wid).resolve()
    for bad in ("..", "W-../../x", "W-" + "0" * 32 + "/../..", "C:\\Windows"):
        with pytest.raises(ValueError):
            workspace_dir(tmp_path, bad)
        with pytest.raises(ValueError):
            snapshot_object_key(bad, 1)
        with pytest.raises(ValueError):
            manifest_object_key(bad)
    assert snapshot_object_key(wid, 3) == f"workspaces/{wid}/snapshot-000000000003.tar.gz"
    assert manifest_object_key(wid) == f"workspaces/{wid}/manifest.json"
    for gen in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            snapshot_object_key(wid, gen)


# ------------------------------------------------------------------------------------------- separation


def test_the_offline_application_never_imports_the_demo_package():
    found = []
    for path in sorted((REPO / "ledgerspy").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(n == "ledgerspy_demo" or n.startswith("ledgerspy_demo.") for n in names):
                found.append(str(path.relative_to(REPO)))
    assert found == []


def test_the_offline_bundle_does_not_ship_the_demo_package_or_tests():
    sys.path.insert(0, str(REPO / "packaging"))
    try:
        import build_bundle
    finally:
        sys.path.remove(str(REPO / "packaging"))
    files = build_bundle.collect(None)
    assert not [f for f in files if f.startswith(("ledgerspy_demo/", "tests/", "docs/demo/", "deploy/"))]
    assert not {"render.yaml", ".dockerignore"} & set(files)  # demo deployment files never ship offline
    assert any(f.startswith("ledgerspy/") for f in files)  # the offline application is still collected
    assert "ledgerspy/cache.py" in files  # the engines' namespaced caches ship with the offline package


def test_the_offline_server_settings_are_unchanged():
    s = load_config().settings
    assert s.server.host == "127.0.0.1"
    assert (
        s.ingest.max_file_bytes == 104_857_600 and s.ingest.max_rows == 1_000_000 and s.pdf.max_pages == 200
    )


def test_the_demo_design_document_passes_the_documentation_checks():
    """docs/demo/ is outside the docs/*.md set checked by tests/backend/test_docs_p23.py (and the offline
    bundle); the same link and machine-path/token checks are applied to it here."""
    sys.path.insert(0, str(REPO / "tests" / "backend"))
    try:
        import test_docs_p23 as docs_checks
    finally:
        sys.path.remove(str(REPO / "tests" / "backend"))
    doc = REPO / "docs" / "demo" / "public-demo.md"
    docs_checks.test_relative_links_resolve(doc)
    docs_checks.test_no_machine_paths_or_tokens_in_documents(doc)
