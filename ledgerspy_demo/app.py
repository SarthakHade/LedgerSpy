"""Assemble the hosted demo: the demo copy of the UI, the synthetic seed, the workspace manager and the front
door. Nothing here is used by the offline application."""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ledgerspy import storage_limits
from ledgerspy.config import load_config
from ledgerspy_demo.allowances import PDF_WORKER_FSIZE_ENV, pdf_render_bytes
from ledgerspy_demo.datadir import DataDirError, DataDirLock, DemoTempFiles, prepare_data_dir
from ledgerspy_demo.frontdoor import FrontDoor
from ledgerspy_demo.frontend import build_demo_frontend
from ledgerspy_demo.persistence import Persistence
from ledgerspy_demo.seed import build_seed
from ledgerspy_demo.settings import DemoSettings
from ledgerspy_demo.storage import SupabaseStorage
from ledgerspy_demo.workspace import WorkspaceManager


@dataclass
class DemoProcess:
    """The process-wide state of a configured demo: its data folder, ownership lock and temporary files."""

    data_dir: Path
    lock: DataDirLock
    temp: DemoTempFiles
    os_temp_dir: (
        str  # the OS temporary folder before it was redirected (what the data folder was checked against)
    )


_PROCESS: DemoProcess | None = None


def configure_process(settings: DemoSettings) -> DemoProcess:
    """Process-wide start-up, once per process (a later call for the same folder returns the same state):
    1. the data folder is validated and marked as demo-owned (refused, nothing deleted, otherwise); the OS
       temporary folder is still the original one, so it is the one the folder is checked against;
    2. the folder's exclusive lock is taken before any cleanup (a second process fails closed);
    3. temporary files of this process and its subprocesses go to a new marked run folder in `<data>/tmp`,
       measured by the storage budget; only earlier runs' marked folders are removed;
    4. the PDF worker subprocess gets its file-size limit (`RLIMIT_FSIZE`, Linux), and the worker policy: each
       worker runs in its own process group with its own folder under `<run>/workers` (also Tesseract's
       temporary folder); a timeout kills the whole tree and waits for it before that folder is deleted.
    On any failure after the lock is taken, the lock is released before the error propagates.
    """
    global _PROCESS
    wanted = Path(os.path.abspath(settings.data_dir))
    if _PROCESS is not None:
        if os.path.normcase(str(_PROCESS.data_dir)) == os.path.normcase(str(wanted.resolve())):
            return _PROCESS
        raise DataDirError(f"this process already runs the demo in {_PROCESS.data_dir}")
    os_temp = tempfile.gettempdir()  # captured before it is redirected below
    data = prepare_data_dir(settings.data_dir)
    lock = DataDirLock(data)
    lock.acquire()
    try:
        temp = DemoTempFiles(data)
        temp.start()
        os.environ[PDF_WORKER_FSIZE_ENV] = str(pdf_render_bytes(settings.limits))
        workers = temp.run_dir / "workers"
        workers.mkdir()
        if workers.resolve().parent != temp.run_dir.resolve():  # inside this process's marked run folder
            raise DataDirError("the PDF worker folder is not inside the run folder")
        storage_limits.set_worker_temp_root(workers)
    except BaseException:
        storage_limits.set_worker_temp_root(None)
        lock.release()
        raise
    _PROCESS = DemoProcess(data_dir=data, lock=lock, temp=temp, os_temp_dir=os_temp)
    return _PROCESS


def release_process() -> None:
    """Release the data folder's lock (normal shutdown, or a start-up that failed after configuration)."""
    global _PROCESS
    process, _PROCESS = _PROCESS, None
    if process is not None:
        storage_limits.set_worker_temp_root(None)
        process.lock.release()


def build_demo_app(settings: DemoSettings, seed_dir: Path | None = None) -> FrontDoor:
    """Build the demo. Every entry point goes through here, so the same rules always apply: limits are checked
    against the offline configuration (stricter only), then the process is configured (`configure_process`:
    data folder checks, ownership lock, temporary files, PDF worker limit) before anything is written."""
    settings.limits.check_against_offline(load_config().settings)
    data = configure_process(settings).data_dir
    frontend = build_demo_frontend(data / "frontend", settings.limits.retention_days, settings.persistent)
    if seed_dir is None and settings.seed_dir is not None:
        seed_dir = Path(settings.seed_dir)
    seed = Path(seed_dir) if seed_dir is not None else data / "seed"
    manager = WorkspaceManager(settings, seed, frontend)
    build_seed(seed, manager.config_for)
    # snapshots only with complete storage settings; otherwise persistence is reported as disabled
    storage = SupabaseStorage(settings.storage) if settings.storage is not None else None
    manager.persistence = Persistence(manager, storage, temp_root=configure_process(settings).temp.run_dir)
    return FrontDoor(settings, manager)
