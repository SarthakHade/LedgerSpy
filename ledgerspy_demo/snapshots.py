"""Workspace snapshots: a consistent, validated, self-describing archive of one visitor workspace.

Building (`build_snapshot`) is local and must run while nothing writes to the workspace (the caller holds the
workspace's request lock): the database is copied with SQLite's online backup API, never as a file, and only
the workspace's persistent files are added (allow-listed names; never temporary files, locks or journals).

Restoring (`extract_snapshot`) trusts nothing in the archive. Before anything is written it checks the
archive's
size and SHA-256 against the pointer it came from, then every entry: allow-listed name, regular file (no
symbolic link or directory entry), no absolute path, `..` or backslash, no duplicate, bounded count and total
size. Files are streamed out with their size capped, hashed and compared with the manifest, into a new, empty
folder that the caller then verifies (audit chain, triggers, evidence) and activates atomically.
"""

import hashlib
import json
import os
import re
import sqlite3
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ledgerspy.cases.db import SCHEMA_VERSION
from ledgerspy_demo.identity import WORKSPACE_ID

FORMAT = "ledgerspy-demo-snapshot"
VERSION = 1
MANIFEST = "snapshot.json"
MAX_FILES = 500
MAX_MANIFEST_BYTES = 256 * 1024
_CHUNK = 1 << 16
# The persistent content of a workspace, and nothing else (tmp/, backups/, journals, locks are never saved).
ALLOWED = re.compile(
    r"^(ledgerspy\.db|workspace\.json|seed\.json"
    r"|evidence/[0-9a-f]{64}(\.[a-z0-9]{1,8})?"
    r"|reports/RP-[0-9A-HJKMNP-TV-Z]{26}\.(html|json|md)"
    r"|inference/I-[0-9a-f]{16}\.json)$"
)
READ_ONLY_DIRS = ("evidence", "reports")  # written read-only by the application; restored the same way


class SnapshotError(Exception):
    """An archive or manifest that must not be used; `reason` is safe to show (no paths, no keys)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Snapshot:
    archive: Path
    sha256: str
    size: int
    manifest: dict


def _sha256_file(path: Path) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _persistent_files(workspace: Path) -> list[str]:
    """Allow-listed regular files of the workspace (links and anything unexpected are left out)."""
    out = []
    for name in ("workspace.json", "seed.json"):
        p = workspace / name
        if p.is_file() and not p.is_symlink():
            out.append(name)
    for sub in ("evidence", "reports", "inference"):
        folder = workspace / sub
        if not folder.is_dir() or folder.is_symlink():
            continue
        for entry in sorted(os.scandir(folder), key=lambda e: e.name):
            rel = f"{sub}/{entry.name}"
            if entry.is_file(follow_symlinks=False) and ALLOWED.fullmatch(rel):
                out.append(rel)
    return out


def build_snapshot(
    workspace: Path,
    workspace_id: str,
    generation: int,
    created_at: float,
    workspace_created_at: float,
    max_bytes: int,
    staging: Path,
) -> Snapshot:
    """Write `snapshot-<generation>.zip` into `staging` (an existing, empty, process-owned folder)."""
    if not WORKSPACE_ID.fullmatch(workspace_id):
        raise SnapshotError("invalid workspace")
    db_copy = staging / "ledgerspy.db"
    src = sqlite3.connect(f"{(workspace / 'ledgerspy.db').resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(db_copy)
        try:
            src.backup(dst)  # a transactionally consistent copy, whatever happens to the source meanwhile
            version = dst.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]  # of the copy
        finally:
            dst.close()
    finally:
        src.close()
    files = [("ledgerspy.db", db_copy)] + [(rel, workspace / rel) for rel in _persistent_files(workspace)]
    if len(files) > MAX_FILES:
        raise SnapshotError("the workspace holds too many files to save")
    entries, total = [], 0
    for rel, path in files:
        digest, size = _sha256_file(path)
        total += size
        entries.append({"path": rel, "size": size, "sha256": digest})
    if total > max_bytes:
        raise SnapshotError("the workspace is larger than a snapshot may be")
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "workspace_id": workspace_id,
        "generation": generation,
        "created_at": created_at,
        "workspace_created_at": workspace_created_at,
        "schema_version": version,
        "total_bytes": total,
        "files": entries,
    }
    archive = staging / f"snapshot-{generation:012d}.zip"
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, json.dumps(manifest, sort_keys=True))
        for rel, path in files:
            z.write(path, rel)
    digest, size = _sha256_file(archive)
    return Snapshot(archive, digest, size, manifest)


def validate_manifest(manifest: object, workspace_id: str, max_bytes: int) -> dict:
    if not isinstance(manifest, dict):
        raise SnapshotError("the saved copy has no valid description")
    if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise SnapshotError("the saved copy has an unknown format")
    if manifest.get("workspace_id") != workspace_id:
        raise SnapshotError("the saved copy belongs to another workspace")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError("the saved copy was made by an incompatible version of LedgerSpy")
    gen = manifest.get("generation")
    if isinstance(gen, bool) or not isinstance(gen, int) or gen < 1:
        raise SnapshotError("the saved copy has an invalid generation")
    for key in ("created_at", "workspace_created_at"):
        if not isinstance(manifest.get(key), (int, float)) or isinstance(manifest.get(key), bool):
            raise SnapshotError("the saved copy has invalid times")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise SnapshotError("the saved copy lists an invalid set of files")
    seen, total = set(), 0
    for f in files:
        if not isinstance(f, dict) or set(f) != {"path", "size", "sha256"}:
            raise SnapshotError("the saved copy lists an invalid file")
        path, size, digest = f["path"], f["size"], f["sha256"]
        if not isinstance(path, str) or not ALLOWED.fullmatch(path) or path in seen:
            raise SnapshotError("the saved copy lists an unexpected or repeated file")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SnapshotError("the saved copy lists an invalid file size")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SnapshotError("the saved copy lists an invalid file hash")
        seen.add(path)
        total += size
    if "ledgerspy.db" not in seen:
        raise SnapshotError("the saved copy has no database")
    if total > max_bytes or manifest.get("total_bytes") != total:
        raise SnapshotError("the saved copy is larger than allowed or inconsistent")
    return manifest


def _safe_name(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/") or "\x00" in name:
        return False
    parts = PurePosixPath(name).parts
    return ".." not in parts and "." not in parts and not PurePosixPath(name).is_absolute()


def extract_snapshot(
    archive: Path, expected_sha256: str, expected_size: int, workspace_id: str, max_bytes: int, dest: Path
) -> dict:
    """Validate `archive` and extract it into `dest` (an existing, empty folder). Returns the manifest."""
    digest, size = _sha256_file(archive)
    if size != expected_size or digest != expected_sha256:
        raise SnapshotError("the saved copy is damaged (its checksum does not match)")
    try:
        z = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise SnapshotError("the saved copy is not a valid archive") from exc
    with z:
        infos = z.infolist()
        if len(infos) > MAX_FILES + 1:
            raise SnapshotError("the saved copy holds too many files")
        names = [i.filename for i in infos]
        if len(set(names)) != len(names):
            raise SnapshotError("the saved copy holds a repeated file")
        declared = 0
        for i in infos:
            mode = (i.external_attr >> 16) & 0o170000
            if not _safe_name(i.filename) or i.is_dir() or mode not in (0, stat.S_IFREG):
                raise SnapshotError("the saved copy holds an unsafe or unexpected entry")
            if i.filename != MANIFEST and not ALLOWED.fullmatch(i.filename):
                raise SnapshotError("the saved copy holds an unexpected file")
            if i.flag_bits & 0x1:
                raise SnapshotError("the saved copy holds an encrypted entry")
            declared += i.file_size
        if MANIFEST not in names or declared > max_bytes + MAX_MANIFEST_BYTES:
            raise SnapshotError("the saved copy is incomplete or too large")
        if z.getinfo(MANIFEST).file_size > MAX_MANIFEST_BYTES:
            raise SnapshotError("the saved copy's description is too large")
        try:
            manifest = json.loads(z.read(MANIFEST))
        except (ValueError, zipfile.BadZipFile) as exc:
            raise SnapshotError("the saved copy has no valid description") from exc
        manifest = validate_manifest(manifest, workspace_id, max_bytes)
        listed = {f["path"]: f for f in manifest["files"]}
        if set(listed) != set(names) - {MANIFEST}:
            raise SnapshotError("the saved copy does not match its description")
        for rel, meta in listed.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.resolve().parent != (dest / PurePosixPath(rel).parent).resolve():
                raise SnapshotError("the saved copy holds an unsafe path")
            h, n = hashlib.sha256(), 0
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            try:
                with os.fdopen(fd, "wb") as out, z.open(rel) as src:
                    while chunk := src.read(_CHUNK):
                        n += len(chunk)
                        if n > meta["size"]:
                            raise SnapshotError("a saved file is larger than described")
                        h.update(chunk)
                        out.write(chunk)
            except zipfile.BadZipFile as exc:
                raise SnapshotError("the saved copy is damaged") from exc
            if n != meta["size"] or h.hexdigest() != meta["sha256"]:
                raise SnapshotError("a saved file does not match its checksum")
            if rel.split("/", 1)[0] in READ_ONLY_DIRS:
                os.chmod(target, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    return manifest
