"""Evidence preservation (docs/architecture.md §12).

Every accepted upload is stored once, content-addressed, as `<workspace>/evidence/<sha256><ext>`, and marked
read-only. Its SHA-256 is computed over the exact bytes received, while they stream in, and recorded in
`evidence_files` (immutable rows). The application only ever opens evidence for reading, and re-hashes it
before every canonicalisation. The original filename is metadata only; it is never used as a path.
"""

import hashlib
import os
import secrets
import sqlite3
import stat
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from ledgerspy.cases import audit
from ledgerspy.hashing import utc_now_iso
from ledgerspy.ingest.errors import IngestError

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class StagedUpload:
    path: Path
    sha256: str
    size_bytes: int


def evidence_dir(workspace: Path) -> Path:
    return workspace / "evidence"


def staging_dir(workspace: Path) -> Path:
    return workspace / "tmp"


def _new_staging_file(workspace: Path) -> Path:
    staging_dir(workspace).mkdir(parents=True, exist_ok=True)
    return staging_dir(workspace) / f"upload-{secrets.token_hex(16)}.part"


def _too_large(limit: int) -> IngestError:
    return IngestError(
        "file_too_large",
        f"The file exceeds the {limit}-byte upload limit.",
        {"limit_bytes": limit},
        http_status=413,
    )


class _Stager:
    """Writes a staging file while hashing; aborts as soon as the size limit is exceeded."""

    def __init__(self, workspace: Path, max_bytes: int) -> None:
        self.path = _new_staging_file(workspace)
        self.max_bytes = max_bytes
        self.digest = hashlib.sha256()
        self.size = 0
        self.out = self.path.open("xb")

    def feed(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.max_bytes:
            raise _too_large(self.max_bytes)
        self.digest.update(chunk)
        self.out.write(chunk)

    def finish(self) -> StagedUpload:
        self.out.close()
        if self.size == 0:
            self.path.unlink(missing_ok=True)
            raise IngestError("empty_file", "The file is empty.")
        return StagedUpload(self.path, self.digest.hexdigest(), self.size)

    def abort(self) -> None:
        self.out.close()
        self.path.unlink(missing_ok=True)


async def stage_stream(chunks: AsyncIterator[bytes], workspace: Path, max_bytes: int) -> StagedUpload:
    """Stage an HTTP upload body chunk by chunk."""
    stager = _Stager(workspace, max_bytes)
    try:
        async for chunk in chunks:
            stager.feed(chunk)
    except BaseException:
        stager.abort()
        raise
    return stager.finish()


def stage_file(source: Path, workspace: Path, max_bytes: int) -> StagedUpload:
    """Stage a local file (tests, future CLI import) with the same hashing and limit."""
    stager = _Stager(workspace, max_bytes)
    try:
        with source.open("rb") as f:
            while chunk := f.read(_CHUNK):
                stager.feed(chunk)
    except BaseException:
        stager.abort()
        raise
    return stager.finish()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _make_read_only(path: Path) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


def store(
    conn: sqlite3.Connection, staged: StagedUpload, ext: str, workspace: Path, actor: str
) -> tuple[str, bool]:
    """Move a staged upload into evidence storage. Must run inside `db.transaction()`.

    Returns (stored_name, newly_stored). An identical file already in evidence is reused (after checking that
    the stored copy still has the recorded hash) and the staged copy is discarded.
    """
    if not conn.in_transaction:
        raise RuntimeError("evidence.store must run inside a transaction")
    target_dir = evidence_dir(workspace)
    target_dir.mkdir(parents=True, exist_ok=True)
    row = conn.execute("SELECT stored_name FROM evidence_files WHERE sha256 = ?", (staged.sha256,)).fetchone()
    if row is not None:
        existing = target_dir / row[0]
        if not existing.is_file() or file_sha256(existing) != staged.sha256:
            raise IngestError(
                "evidence_tampered",
                f"Stored evidence {row[0]} no longer matches its recorded SHA-256.",
                http_status=409,
            )
        staged.path.unlink(missing_ok=True)
        return row[0], False
    stored_name = staged.sha256 + ext.lower()
    target = target_dir / stored_name
    if target.exists():  # a file without a DB row: never trust or overwrite it
        raise IngestError(
            "evidence_conflict", f"An unregistered file already occupies {stored_name}.", http_status=409
        )
    os.replace(staged.path, target)
    _make_read_only(target)
    if file_sha256(target) != staged.sha256:  # pragma: no cover — defensive: the move must not alter bytes
        raise IngestError("evidence_write_failed", "The stored evidence copy does not match the upload hash.")
    conn.execute(
        "INSERT INTO evidence_files (sha256, size_bytes, stored_name, stored_at) VALUES (?, ?, ?, ?)",
        (staged.sha256, staged.size_bytes, stored_name, utc_now_iso()),
    )
    audit.append(
        conn,
        actor,
        "evidence.stored",
        f"evidence:{staged.sha256}",
        {"sha256": staged.sha256, "size_bytes": staged.size_bytes, "stored_name": stored_name},
    )
    return stored_name, True


def evidence_path(conn: sqlite3.Connection, sha256: str, workspace: Path) -> Path:
    row = conn.execute("SELECT stored_name FROM evidence_files WHERE sha256 = ?", (sha256,)).fetchone()
    if row is None:
        raise IngestError("evidence_missing", f"No evidence record for {sha256}.", http_status=409)
    return evidence_dir(workspace) / row[0]


def verify_one(conn: sqlite3.Connection, sha256: str, workspace: Path) -> Path:
    """Return the evidence path after checking the stored bytes still hash to the recorded value."""
    path = evidence_path(conn, sha256, workspace)
    if not path.is_file():
        raise IngestError("evidence_missing", f"Evidence file {path.name} is missing.", http_status=409)
    if file_sha256(path) != sha256:
        raise IngestError(
            "evidence_tampered", f"Evidence file {path.name} no longer matches its SHA-256.", http_status=409
        )
    return path


def verify_all(conn: sqlite3.Connection, workspace: Path) -> list[dict[str, str]]:
    """Check every evidence file. Returns one problem dict per bad file (empty list = all intact)."""
    problems = []
    for sha, name, size in conn.execute(
        "SELECT sha256, stored_name, size_bytes FROM evidence_files ORDER BY sha256"
    ):
        path = evidence_dir(workspace) / name
        if not path.is_file():
            problems.append({"sha256": sha, "file": name, "problem": "missing"})
        elif path.stat().st_size != size or file_sha256(path) != sha:
            problems.append({"sha256": sha, "file": name, "problem": "hash mismatch"})
        elif os.access(path, os.W_OK):
            problems.append({"sha256": sha, "file": name, "problem": "not read-only"})
    return problems
