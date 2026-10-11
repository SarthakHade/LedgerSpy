"""Runtime safety and diagnostics for an installed LedgerSpy (P21).

* `WorkspaceLock`: one server per workspace. An OS file lock is held on `<workspace>/.ledgerspy.lock` while
  `ledgerspy serve` runs; a second server on the same workspace is refused. The OS releases the lock when the
  process ends, so a crash leaves no stale lock.
* `port_available`: a clear error before startup instead of a failed bind.
* `doctor`: checks an installation without changing anything (Python, pinned dependencies, the UI files and
  strict CSP, the configuration, the workspace, the database schema, the PDF renderer and the local
  Tesseract).
"""

import importlib.metadata
import os
import re
import socket
import sqlite3
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ledgerspy.paths import FRONTEND_DIR, REPO_ROOT

LOCK_NAME = ".ledgerspy.lock"


class WorkspaceLocked(RuntimeError):
    pass


class WorkspaceLock:
    """Exclusive, non-blocking lock on `<workspace>/.ledgerspy.lock`, held for the life of the server."""

    def __init__(self, workspace: Path) -> None:
        self.path = Path(workspace) / LOCK_NAME
        self._fh = None

    def acquire(self) -> "WorkspaceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # noqa: SIM115 — kept open while the lock is held
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise WorkspaceLocked(
                f"another LedgerSpy server is already using the workspace {self.path.parent}; stop it first "
                "(two servers on one database are refused)"
            ) from exc
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "WorkspaceLock":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()


def port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name == "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


# ------------------------------------------------------------------------------------------- doctor


@dataclass
class Check:
    name: str
    status: str  # ok / warn / fail
    detail: str


def pinned_dependencies() -> dict[str, str]:
    """Runtime pins from pyproject.toml (the single source; requirements.lock adds the transitive ones)."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        deps = tomllib.load(f)["project"]["dependencies"]
    out = {}
    for d in deps:
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^;\s]+)", d)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def locked_dependencies() -> dict[str, str]:
    """All pins of requirements.lock (runtime closure), if the file exists."""
    lock = REPO_ROOT / "requirements.lock"
    if not lock.is_file():
        return {}
    out = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def doctor(
    config_path: str | None = None, workspace: str | None = None, port: int | None = None
) -> list[Check]:
    checks: list[Check] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append(Check(name, status, detail))

    v = sys.version_info
    add("python", "ok" if v >= (3, 11) else "fail", f"{sys.version.split()[0]} at {sys.executable}")

    wanted = locked_dependencies() or pinned_dependencies()
    bad = []
    for name, version in sorted(wanted.items()):
        try:
            have = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            bad.append(f"{name} missing (need {version})")
            continue
        if _norm(have) != _norm(version):
            bad.append(f"{name} {have} (need {version})")
    source = "requirements.lock" if locked_dependencies() else "pyproject.toml"
    add(
        "dependencies",
        "fail" if bad else "ok",
        "; ".join(bad) if bad else f"{len(wanted)} packages match {source}",
    )

    page = FRONTEND_DIR / "index.html"
    if not page.is_file():
        add("frontend", "fail", f"{page} is missing (run from the LedgerSpy application folder)")
    else:
        from ledgerspy.api.security import inline_markup

        bad_markup = inline_markup(page.read_text(encoding="utf-8"))
        add(
            "frontend",
            "fail" if bad_markup else "ok",
            ", ".join(bad_markup) or "UI present; strict CSP holds",
        )

    from ledgerspy.config import ConfigError, load_config

    overrides: dict[str, Any] = {}
    if workspace:
        overrides["workspace"] = {"path": workspace}
    if port:
        overrides["server"] = {"port": port}
    try:
        cfg = load_config(config_path, overrides or None)
    except ConfigError as exc:
        add("configuration", "fail", str(exc))
        return checks
    s = cfg.settings
    add(
        "configuration",
        "ok",
        f"{cfg.source} (sha256 {cfg.sha256[:16]}...); server {s.server.host}:{s.server.port}",
    )

    ws = Path(s.workspace.path).resolve()
    try:
        # nothing is created: a missing workspace is checked through its nearest existing parent
        existing = next(p for p in (ws, *ws.parents) if p.exists())
        probe = existing / f".ledgerspy-doctor-{os.getpid()}"
        probe.write_bytes(b"x")
        probe.unlink()
        inside = REPO_ROOT.resolve() in ws.parents or ws == REPO_ROOT.resolve()
        add(
            "workspace",
            "warn" if inside else "ok",
            (f"{ws} is writable" if ws.exists() else f"{ws} does not exist yet; it will be created")
            + (
                "; it is inside the application folder: an upgrade that replaces the folder would remove it"
                if inside
                else ""
            ),
        )
    except OSError as exc:
        add("workspace", "fail", f"{ws} is not writable: {exc}")
        return checks

    from ledgerspy.cases.db import SCHEMA_VERSION, current_version, db_path_for

    db = db_path_for(s).resolve()
    if not db.is_file():
        add("database", "ok", f"{db} will be created (schema v{SCHEMA_VERSION})")
    else:
        try:
            conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
            try:
                have = current_version(conn)
                integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as exc:
            add("database", "fail", f"{db} cannot be read: {exc}")
        else:
            if integrity != "ok":
                add("database", "fail", f"{db} fails SQLite quick_check: {integrity}")
            elif have > SCHEMA_VERSION:
                add("database", "fail", f"schema v{have} is newer than this LedgerSpy (v{SCHEMA_VERSION})")
            elif have < SCHEMA_VERSION:
                add("database", "ok", f"schema v{have}; will be upgraded to v{SCHEMA_VERSION} after a backup")
            else:
                add("database", "ok", f"{db} schema v{have} (current)")

    if not (ws / LOCK_NAME).exists():  # a running server always holds this file; none: nothing is running
        add("workspace lock", "ok", "no LedgerSpy server has used this workspace yet")
    else:
        try:
            WorkspaceLock(ws).acquire().release()
            add("workspace lock", "ok", "no other LedgerSpy server is using this workspace")
        except WorkspaceLocked as exc:
            add("workspace lock", "warn", str(exc))

    host, p = s.server.host, s.server.port
    add(
        "port",
        "ok" if port_available(host, p) else "warn",
        f"{host}:{p} is free" if port_available(host, p) else f"{host}:{p} is in use (another server?)",
    )

    try:
        import PIL
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument.new()
        pdf.new_page(200, 100)
        bmp = pdf[0].render(scale=1).to_pil()
        pdf.close()
        add(
            "pdf renderer",
            "ok",
            f"pypdfium2 {importlib.metadata.version('pypdfium2')}, Pillow {PIL.__version__}; "
            f"rendered a test page {bmp.size[0]}x{bmp.size[1]}",
        )
    except Exception as exc:  # noqa: BLE001 — any failure of the native PDF stack is reported, not raised
        add("pdf renderer", "fail", f"{type(exc).__name__}: {exc}")

    from ledgerspy.pdfstatements import ocr

    eng = ocr.discover(s.pdf.ocr.model_dump(mode="json"))
    if eng.available:
        add(
            "ocr",
            "ok",
            f"Tesseract {eng.version} at {eng.executable}; languages {', '.join(eng.installed_languages)}",
        )
    else:
        add(
            "ocr",
            "warn",
            f"OCR unavailable: {eng.reason}. Text PDFs, CSV and XLSX still work; scanned pages "
            "are reported as not processed",
        )
    return checks
