"""Optional, process-wide storage limits per workspace: hard bounds that a host can place on disk growth.

Nothing in the offline application registers a limit, so offline every function here is a no-op and the
behaviour is unchanged. The hosted demo (`ledgerspy_demo`, never shipped offline) registers, for the request
it is about to run in a workspace:

- a database size limit: every connection to that database then gets `PRAGMA max_page_count` (SQLite refuses,
  with SQLITE_FULL, any write that would grow the file beyond it, and the transaction rolls back),
  `PRAGMA temp_store = MEMORY` (no SQLite temporary files on disk) and `PRAGMA cache_spill = OFF` (dirty pages
  stay in memory until commit, so the rollback journal is one segment with one header: its size is bounded by
  the pages it journals; each spill would otherwise add a sector-sized header);
- a file-output allowance: report artifacts and stored inference results are checked against it before they
  are written, and refused (nothing written) when they do not fit;
- read-only database access (`PRAGMA query_only`) for requests that must not write;
- through the environment (`WORKER_FILE_LIMIT_ENV`), a file-size limit for the PDF worker subprocess
  (`RLIMIT_FSIZE`, POSIX only), which bounds every temporary page image it writes;
- a worker policy (`set_worker_temp_root`): each PDF worker then runs in its own process group / session
  with its own temporary folder (passed to Tesseract too, `WORKER_TEMP_ENV`); on a timeout or at shutdown
  (`kill_all_workers`) the whole process tree is killed and waited for, and only then is that folder deleted.
"""

import contextlib
import math
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ledgerspy.errors import LedgerSpyError

WORKER_FILE_LIMIT_ENV = "LEDGERSPY_PDF_WORKER_MAX_FILE_BYTES"
WORKER_TEMP_ENV = "LEDGERSPY_PDF_WORKER_TEMP_DIR"

_lock = threading.Lock()
_db_max_bytes: dict[str, tuple[int, bool]] = {}  # database -> (max bytes, read only)
_output_left: dict[str, int] = {}


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def set_db_max_bytes(db_path: Path | str, max_bytes: int | None, read_only: bool = False) -> None:
    """Limit the database file to `max_bytes` (and optionally to reads) for connections opened from now on.
    None removes the limit."""
    with _lock:
        if max_bytes is None:
            _db_max_bytes.pop(_key(db_path), None)
        else:
            _db_max_bytes[_key(db_path)] = (max(0, int(max_bytes)), read_only)


def db_limit(db_path: Path | str) -> tuple[int, bool] | None:
    with _lock:
        return _db_max_bytes.get(_key(db_path))


def apply_db_limit(conn: sqlite3.Connection, db_path: Path | str) -> None:
    """Called by `cases.db.connect` for every new connection."""
    limit = db_limit(db_path)
    if limit is None:
        return
    max_bytes, read_only = limit
    page = conn.execute("PRAGMA page_size").fetchone()[0]
    # SQLite never lowers max_page_count below the current page count: an over-full file cannot grow further
    conn.execute(f"PRAGMA max_page_count = {max(1, math.floor(max_bytes / page))}")
    conn.execute("PRAGMA temp_store = MEMORY")
    # one journal segment: memory cost at most the transaction's dirty pages, i.e. the database cap
    conn.execute("PRAGMA cache_spill = OFF")
    if read_only:
        conn.execute("PRAGMA query_only = ON")


def set_output_allowance(workspace: Path | str, nbytes: int | None) -> None:
    """The file output the next operation in `workspace` may write (None: no limit)."""
    with _lock:
        if nbytes is None:
            _output_left.pop(_key(workspace), None)
        else:
            _output_left[_key(workspace)] = max(0, int(nbytes))


def claim_output(workspace: Path | str, nbytes: int, what: str) -> None:
    """Account for `nbytes` of file output before writing it; refused (nothing claimed) when over."""
    with _lock:
        key = _key(workspace)
        left = _output_left.get(key)
        if left is None:
            return
        if nbytes > left:
            raise LedgerSpyError(
                "storage_limit",
                f"The {what} ({nbytes} bytes) is larger than this workspace may write in one operation "
                f"({left} bytes); nothing was written.",
                {"bytes": nbytes, "limit_bytes": left},
                http_status=507,
            )
        _output_left[key] = left - nbytes


def clear(workspace: Path | str, db_path: Path | str) -> None:
    set_output_allowance(workspace, None)
    set_db_max_bytes(db_path, None)


def apply_worker_file_limit() -> bool:
    """In the PDF worker subprocess: cap the size of any file it writes, if the host asked for it (POSIX only;
    the kernel then stops the worker, so the extraction fails, rather than let a file grow past the cap)."""
    raw = os.environ.get(WORKER_FILE_LIMIT_ENV, "").strip()
    if not raw.isdigit():
        return False
    try:
        import resource
    except ImportError:  # Windows: not enforced
        return False
    resource.setrlimit(resource.RLIMIT_FSIZE, (int(raw), int(raw)))
    return True


# --------------------------------------------------------------------------------------- PDF worker policy
_worker_temp_root: Path | None = None
_live_workers: set[subprocess.Popen] = set()


def set_worker_temp_root(root: Path | str | None) -> None:
    """Enable (a folder) or disable (None) the worker policy. The folder must exist; it is resolved once."""
    global _worker_temp_root
    with _lock:
        _worker_temp_root = None if root is None else Path(root).resolve(strict=True)


def worker_temp_root() -> Path | None:
    with _lock:
        return _worker_temp_root


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill a worker and every process it started (it runs in its own process group / session)."""
    if os.name == "nt":
        if proc.poll() is not None:
            return
        taskkill = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe")
        subprocess.run(  # noqa: S603 — the system's taskkill by absolute path, numeric PID
            [taskkill, "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30, check=False
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


def _group_gone(pgid: int, timeout: float) -> bool:
    """POSIX: wait until no process of the group is left (its files are then closed)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def kill_all_workers() -> int:
    """Kill every running worker tree (graceful shutdown past its deadline). Returns how many were running."""
    with _lock:
        procs = list(_live_workers)
    for proc in procs:
        kill_tree(proc)
    return len(procs)


def live_workers() -> int:
    with _lock:
        return len(_live_workers)


def run_worker(
    cmd: list[str], stdin_text: str, timeout: float, env: dict[str, str], cwd: str
) -> subprocess.CompletedProcess:
    """Run a PDF worker under the policy. Raises subprocess.TimeoutExpired after killing the whole tree.
    The worker's temporary folder is removed only after the worker and its descendants have exited."""
    root = worker_temp_root()
    if root is None:
        raise RuntimeError("the worker policy is not enabled")
    work = Path(tempfile.mkdtemp(prefix="pdf-", dir=root))
    if work.resolve().parent != root:  # pragma: no cover — mkdtemp in `root` cannot land elsewhere
        raise RuntimeError("worker temporary folder outside its root")
    env = {**env, "TMPDIR": str(work), "TEMP": str(work), "TMP": str(work), WORKER_TEMP_ENV: str(work)}
    extra: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(  # noqa: S603 — the caller's fixed command
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
        **extra,
    )
    with _lock:
        _live_workers.add(proc)
    gone = True
    try:
        try:
            out, err = proc.communicate(stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.communicate()  # reaps the worker; its pipes close once every holder is gone
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    finally:
        if proc.poll() is None:  # e.g. an exception while communicating
            kill_tree(proc)
            proc.wait()
        if os.name != "nt":
            gone = _group_gone(proc.pid, 10.0)
        with _lock:
            _live_workers.discard(proc)
        if gone:  # never delete files a surviving descendant may still be writing
            shutil.rmtree(work, ignore_errors=True)
