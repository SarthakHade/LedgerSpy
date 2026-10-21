"""The demo's data folder: validated, marked as demo-owned, and the only tree demo cleanup may ever touch.

Start-up order (`__main__`): `check_data_dir_setting` (settings), then `prepare_data_dir`, then
`DemoTempFiles.start`. Nothing is deleted unless every check below holds; anything doubtful refuses to start
(fail closed) and deletes nothing.

- **Setting.** The path must be absolute, without `.`/`..` parts, not a file-system or drive root, not a
  UNC / device path, and not a well-known system folder, the OS temporary folder, the home folder, or an
  ancestor of the repository.
- **Folder.** No part of the existing path may be a symbolic link, junction or other reparse point, and the
  resolved path must equal the lexical one (so nothing redirects it). The folder must either carry the demo
  marker written for exactly this resolved path, or be new / empty, in which case the marker is created with
  O_EXCL. A non-empty folder without a valid marker, or one holding an offline LedgerSpy database, is refused.
- **Ownership.** One process at a time: `DataDirLock` holds an exclusive lock on `<data>/.ledgerspy-demo.lock`
  for the life of the process (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows). It is taken after the
  folder checks and before any cleanup, so a second process fails closed without touching the first one's
  files. The operating system releases it if the process dies.
- **Temporary files.** `<data>/tmp` must be a real folder carrying its own marker. Each run works in a new
  `run-<32 hex>` folder holding a run marker that names it; at start-up only such run folders of earlier runs
  are removed, and nothing else in `<data>/tmp`, which is never deleted as a whole.
- **Deletion.** Links, junctions and reparse points are removed as links, never followed. On POSIX the walk is
  `shutil.rmtree`, which works on directory file descriptors and is safe against symbolic-link races. On
  Windows each entry is re-checked just before it is removed; a race against a local attacker who can write
  inside the data folder remains possible there (the demo is not deployed on Windows).
"""

import json
import os
import re
import secrets
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from ledgerspy.paths import REPO_ROOT
from ledgerspy_demo.limits import DemoConfigError

DATA_MARKER = ".ledgerspy-demo-data"
LOCK_NAME = ".ledgerspy-demo.lock"
TMP_MARKER = ".ledgerspy-demo-tmp"
RUN_MARKER = ".ledgerspy-demo-run"
MARKER_PURPOSE = "ledgerspy-demo-data"
RUN_NAME = re.compile(r"^run-[0-9a-f]{32}$")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SYSTEM_FOLDERS = {  # refused as the data folder (never used)
    "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt", "/proc", "/root", "/run", "/sbin",
    "/srv", "/sys", "/usr", "/var", "/private", "/Users", "/Library", "/System", "/Volumes", "/mnt", "/media",
    "/tmp", "/var/tmp", "/private/tmp",  # noqa: S108
}  # fmt: skip
_WINDOWS_SYSTEM = {"windows", "program files", "program files (x86)", "programdata", "users"}


class DataDirError(DemoConfigError):
    """The demo data folder is unsafe or not demo-owned; nothing was deleted."""


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def check_data_dir_setting(raw: str) -> None:
    """Refuse an unsafe LEDGERSPY_DEMO_DATA_DIR before anything touches the disk (pure: no I/O)."""
    if not raw or raw != raw.strip():
        raise DataDirError("LEDGERSPY_DEMO_DATA_DIR must be a plain absolute path")
    if raw.startswith(("\\\\", "//")):
        raise DataDirError("LEDGERSPY_DEMO_DATA_DIR must not be a UNC, network or device path")
    win = PureWindowsPath(raw)
    posix = PurePosixPath(raw)
    pure = win if win.drive else posix
    if not pure.is_absolute():
        raise DataDirError("LEDGERSPY_DEMO_DATA_DIR must be an absolute path")
    parts = re.split(r"[\\/]+", raw)
    if any(part in (".", "..") for part in parts):
        raise DataDirError("LEDGERSPY_DEMO_DATA_DIR must not contain '.' or '..' parts")
    if len(pure.parts) <= 1:
        raise DataDirError(f"LEDGERSPY_DEMO_DATA_DIR must not be a file-system or drive root ({raw!r})")
    if pure is posix and str(posix).rstrip("/") in _SYSTEM_FOLDERS:
        raise DataDirError(f"LEDGERSPY_DEMO_DATA_DIR must not be a system folder ({raw!r})")
    if pure is win and len(win.parts) == 2 and win.parts[1].lower() in _WINDOWS_SYSTEM:
        raise DataDirError(f"LEDGERSPY_DEMO_DATA_DIR must not be a system folder ({raw!r})")


def is_link_like(path: Path) -> bool:
    """A symbolic link, junction or any other reparse point (never followed or trusted)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def _check_no_links(path: Path) -> None:
    """No existing component of `path` (an absolute, normalised path) may be a link or reparse point."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if not os.path.lexists(current):
            return
        if is_link_like(current):
            raise DataDirError(f"the demo data path goes through a link or junction ({current})")


def _forbidden_targets() -> list[Path]:
    out = [Path(REPO_ROOT)]
    for candidate in (tempfile.gettempdir(), os.path.expanduser("~")):
        if candidate and os.path.isabs(candidate):
            out.append(Path(candidate))
    return out


def _check_not_sensitive(resolved: Path) -> None:
    mine = _norm(resolved)
    for other in _forbidden_targets():
        theirs = _norm(other.resolve() if other.exists() else other)
        if theirs == mine or theirs.startswith(mine.rstrip(os.sep) + os.sep):
            raise DataDirError(
                f"the demo data folder must not be, or contain, {other} (the OS temporary folder, the home "
                "folder or the LedgerSpy repository)"
            )
    if resolved.is_dir() and (resolved / "ledgerspy.db").exists():
        raise DataDirError("the demo data folder holds an offline LedgerSpy database; use an empty folder")


def _marker_body(resolved: Path) -> dict:
    return {"purpose": MARKER_PURPOSE, "path": str(resolved)}


def _read_marker(path: Path, expected: dict) -> None:
    if is_link_like(path) or not path.is_file():
        raise DataDirError(f"{path.name} is not a regular file; the folder is not trusted")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataDirError(f"{path} is not a valid demo marker") from exc
    if body != expected:
        raise DataDirError(f"{path} does not mark this folder (forged, copied or from another folder)")


def _write_new(path: Path, body: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(body, f)


def prepare_data_dir(raw: str) -> Path:
    """Validate the data folder and make sure it is demo-owned; returns its resolved path."""
    check_data_dir_setting(raw)
    lexical = Path(os.path.abspath(raw))
    _check_no_links(lexical)
    _check_not_sensitive(lexical)  # before anything is created, by the path as given
    if not os.path.lexists(lexical):
        if not lexical.parent.is_dir():
            raise DataDirError(f"the parent of the demo data folder does not exist ({lexical.parent})")
        # only the last component, never an unexpected tree; a folder created meanwhile by a concurrent
        # start-up is checked below like any existing folder
        try:
            lexical.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise DataDirError(f"the demo data folder cannot be created ({type(exc).__name__})") from None
    if is_link_like(lexical) or not lexical.is_dir():
        raise DataDirError(f"the demo data path is not a real folder ({lexical})")
    resolved = lexical.resolve(strict=True)
    if _norm(resolved) != _norm(lexical):
        raise DataDirError(
            f"the demo data path resolves elsewhere ({lexical} -> {resolved}); use its real path"
        )
    _check_not_sensitive(resolved)
    marker = resolved / DATA_MARKER
    if os.path.lexists(marker):
        _read_marker(marker, _marker_body(resolved))
    elif any(resolved.iterdir()):
        raise DataDirError(
            f"{resolved} is not empty and has no demo marker; the demo uses only an empty or demo-owned "
            "folder"
        )
    else:
        try:
            _write_new(marker, _marker_body(resolved))
        except FileExistsError:  # a concurrent start-up wrote it first: it must mark this folder exactly
            _read_marker(marker, _marker_body(resolved))
    return resolved


def safe_remove_tree(path: Path, parent: Path) -> None:
    """Delete `path`, a real folder directly inside `parent`, without following any link or junction."""
    if Path(path).parent != Path(parent) or is_link_like(path) or not Path(path).is_dir():
        raise DataDirError(f"refusing to delete {path}: not a real folder directly inside {parent}")
    if shutil.rmtree.avoids_symlink_attacks:  # POSIX: descriptor-based walk, safe against link races
        shutil.rmtree(path)
        return
    _remove_windows(Path(path))


def _remove_windows(folder: Path) -> None:
    with os.scandir(folder) as entries:
        children = list(entries)
    for entry in children:
        p = Path(entry.path)
        if is_link_like(p):  # re-checked just before acting: the link itself goes, its target never
            try:
                os.rmdir(p)
            except OSError:
                os.unlink(p)
        elif entry.is_dir(follow_symlinks=False):
            _remove_windows(p)
        else:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            os.unlink(p)
    if is_link_like(folder):
        raise DataDirError(f"{folder} became a link during deletion; stopped")
    os.rmdir(folder)


class DataDirLock:
    """Exclusive ownership of a demo data folder for the life of the process; a second owner is refused."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / LOCK_NAME
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if is_link_like(self.path):
            raise DataDirError(f"{self.path} is a link; the folder is not trusted")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # byte 0; fails at once if another process holds it
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise DataDirError(
                "another LedgerSpy demo process owns this data folder; refusing to start (nothing cleaned)"
            ) from None
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class DemoTempFiles:
    """The demo's temporary-file manager: one marked run folder per process inside `<data>/tmp`."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.container = self.data_dir / "tmp"
        self.run_dir: Path | None = None
        self.left_alone: list[str] = []

    def _container_marker(self) -> dict:
        return {"purpose": MARKER_PURPOSE, "path": str(self.container)}

    def start(self, set_environment: bool = True) -> Path:
        """Validate (or create) `<data>/tmp`, remove earlier runs' folders, create this run's folder and send
        this process's and its subprocesses' temporary files there."""
        _read_marker(
            self.data_dir / DATA_MARKER, _marker_body(self.data_dir)
        )  # the data folder is demo-owned
        if not os.path.lexists(self.container):
            self.container.mkdir()
            _write_new(self.container / TMP_MARKER, self._container_marker())
        if is_link_like(self.container) or not self.container.is_dir():
            raise DataDirError(f"{self.container} is not a real folder; nothing was cleaned")
        _read_marker(self.container / TMP_MARKER, self._container_marker())
        self.cleanup()
        run = self.container / f"run-{secrets.token_hex(16)}"
        run.mkdir()
        _write_new(run / RUN_MARKER, {"purpose": MARKER_PURPOSE, "run": run.name})
        self.run_dir = run
        if set_environment:
            for name in ("TMPDIR", "TEMP", "TMP"):
                os.environ[name] = str(run)
            tempfile.tempdir = None  # re-read from the environment on next use
        return run

    def cleanup(self) -> list[str]:
        """Remove earlier runs' folders, and only those: proven demo-owned by name, type and run marker."""
        removed, self.left_alone = [], []
        with os.scandir(self.container) as entries:
            children = list(entries)
        for entry in children:
            p = Path(entry.path)
            if entry.name == TMP_MARKER or (self.run_dir is not None and p == self.run_dir):
                continue
            owned = (
                RUN_NAME.fullmatch(entry.name)
                and not is_link_like(p)
                and entry.is_dir(follow_symlinks=False)
                and self._run_marker_ok(p)
            )
            if not owned:
                self.left_alone.append(entry.name)  # ownership not established: never deleted
                continue
            safe_remove_tree(p, self.container)
            removed.append(entry.name)
        return removed

    @staticmethod
    def _run_marker_ok(run: Path) -> bool:
        try:
            _read_marker(run / RUN_MARKER, {"purpose": MARKER_PURPOSE, "run": run.name})
        except DataDirError:
            return False
        return True
