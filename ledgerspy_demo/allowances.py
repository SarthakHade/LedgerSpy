"""What each request may write to disk, and the reservation that bounds it (docs/demo/public-demo.md §6.1).

Every request is classified before it runs. A state-changing request that is not listed here is refused:
a new route is never admitted with an unknown bound.

The bound of an admitted request is the sum of parts that are each enforced, never merely estimated:

- **database growth**: the database file may grow to `db_cap` and no further. SQLite enforces it
  (`PRAGMA max_page_count`, set through `ledgerspy.storage_limits`); a write beyond it fails with
  SQLITE_FULL and its transaction rolls back. Reads run with `PRAGMA query_only`, so they cannot write.
- **rollback journal** (transient): a transaction journals each page that existed when it began at most once,
  so the journal is at most `pages × (page_size + 8)` plus its header (one sector, at most 64 KiB) — at most
  the database's size at the cap. SQLite temporary files are kept in memory (`PRAGMA temp_store = MEMORY`).
- **files**: an upload writes its body once (staged, then moved into evidence); uvicorn holds the body to the
  declared Content-Length and the workspace app enforces its streaming limit. Reports and inference results
  are refused before they are written when larger than their limit (`storage_limits.claim_output`).
- **PDF page renders** (transient, one page at a time): the PDF worker subprocess runs with a file-size
  limit (`RLIMIT_FSIZE`, Linux; see `PDF_WORKER_FSIZE_ENV`), so no file it writes can exceed it.
  On Windows the limit is not enforced and this part is an estimate (the demo is not deployed on Windows).

Persistent workspace growth is at most `db_growth + files`, chosen so that the workspace stays within its
quota after the request.
"""

import os
import re
from dataclasses import dataclass

from ledgerspy.storage_limits import WORKER_FILE_LIMIT_ENV
from ledgerspy_demo.limits import MIB, DemoLimits

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SQLITE_JOURNAL_HEADER_MAX = 65536  # one sector, the largest sector size SQLite assumes
PDF_WORKER_FSIZE_ENV = WORKER_FILE_LIMIT_ENV
# The PDF worker's file-size limit is a POSIX rlimit. Where it cannot be enforced (Windows), the demo renders
# no PDF pages at all: OCR is configured off and page images are refused, so no unbounded file is written.
RENDER_LIMIT_ENFORCED = os.name == "posix"


@dataclass(frozen=True)
class OperationClass:
    name: str
    writes_db: bool
    files: str = "none"  # "none" | "upload" | "report" | "inference"
    pdf_render: str = "never"  # "never" | "always" | "bank_statement_upload"


DB_WRITE = OperationClass("db_write", writes_db=True)
UPLOAD = OperationClass("upload", writes_db=True, files="upload", pdf_render="bank_statement_upload")
PDF_EXTRACT = OperationClass("pdf_extract", writes_db=True, pdf_render="always")
REPORT = OperationClass("report", writes_db=True, files="report")
INFERENCE = OperationClass("inference", writes_db=True, files="inference")
READ = OperationClass("read", writes_db=False)
PDF_PAGE_IMAGE = OperationClass("pdf_page_image", writes_db=False, pdf_render="always")

_ID = r"[^/]+"
# Every state-changing route of the workspace app, with what it writes (checked against the app's routes by
# tests/demo/test_demo_storage_bounds.py). DB_WRITE routes write the database only.
STATE_CHANGING: tuple[tuple[str, str, OperationClass], ...] = (
    ("POST", r"/api/cases", DB_WRITE),
    ("POST", rf"/api/cases/{_ID}/files", UPLOAD),
    ("PUT", rf"/api/datasets/{_ID}/mapping", DB_WRITE),
    ("POST", rf"/api/datasets/{_ID}/integrity", DB_WRITE),
    ("POST", rf"/api/datasets/{_ID}/fixes/decisions", DB_WRITE),
    ("POST", rf"/api/datasets/{_ID}/normalize", DB_WRITE),
    ("POST", rf"/api/datasets/{_ID}/transformations/{_ID}/revert", DB_WRITE),
    ("POST", rf"/api/datasets/{_ID}/forensics", DB_WRITE),
    ("POST", rf"/api/datasets/{_ID}/anomaly", DB_WRITE),
    ("POST", rf"/api/datasets/{_ID}/risk", DB_WRITE),
    ("POST", rf"/api/datasets/{_ID}/supervised/inference", INFERENCE),
    ("POST", rf"/api/datasets/{_ID}/pdf/review", DB_WRITE),
    ("POST", rf"/api/datasets/{_ID}/pdf/extract", PDF_EXTRACT),
    ("POST", rf"/api/datasets/{_ID}/pdf/items/{_ID}/corrections", DB_WRITE),
    ("POST", r"/api/reconciliation/runs", DB_WRITE),
    ("POST", r"/api/investigations", DB_WRITE),
    ("PATCH", rf"/api/investigations/{_ID}", DB_WRITE),
    ("POST", rf"/api/investigations/{_ID}/transitions", DB_WRITE),
    ("POST", rf"/api/investigations/{_ID}/links", DB_WRITE),
    ("POST", rf"/api/investigations/{_ID}/links/{_ID}/unlink", DB_WRITE),
    ("POST", rf"/api/investigations/{_ID}/notes", DB_WRITE),
    ("POST", rf"/api/investigations/{_ID}/decisions", DB_WRITE),
    ("POST", rf"/api/investigations/{_ID}/ocr-corrections", DB_WRITE),
    ("POST", r"/api/reports/preview", DB_WRITE),
    ("POST", r"/api/reports", REPORT),
)
# Reads that write a temporary file (every other GET / HEAD is a plain READ).
TEMP_WRITING_READS: tuple[tuple[str, str, OperationClass], ...] = (
    ("GET", rf"/api/datasets/{_ID}/pdf/pages/{_ID}/image", PDF_PAGE_IMAGE),
)
_COMPILED = [(m, re.compile(f"^{p}$"), c) for m, p, c in STATE_CHANGING + TEMP_WRITING_READS]


def classify(method: str, path: str) -> OperationClass | None:
    """The class of a request, or None for a state-changing request with no known bound (refused)."""
    for m, rx, cls in _COMPILED:
        if m == method and rx.match(path):
            return cls
    return None if method in MUTATING else READ


def pdf_render_bytes(limits: DemoLimits) -> int:
    """The per-file limit of the PDF worker: a grayscale PNG of a page at the pixel limit, with room for row
    filter bytes, deflate framing and PNG chunks (realistic pages need about 1.0 × the pixel count)."""
    return 2 * limits.max_pdf_render_pixels + MIB


def journal_bytes(db_cap: int, page_size: int) -> int:
    pages = -(-db_cap // page_size)
    return pages * (page_size + 8) + SQLITE_JOURNAL_HEADER_MAX


@dataclass(frozen=True)
class StoragePlan:
    op: OperationClass
    db_cap: int  # the database file may not grow beyond this (bytes)
    db_growth: int
    journal: int
    files: int
    render: int

    @property
    def persistent(self) -> int:
        """The most the workspace folder can still hold after the request (beyond its current size)."""
        return self.db_growth + self.files

    @property
    def total(self) -> int:
        """The reservation: everything the request can have on disk at once beyond current usage."""
        return self.db_growth + self.journal + self.files + self.render


class InsufficientRoom(Exception):
    def __init__(self, needed: int, room: int) -> None:
        super().__init__(f"needs {needed} bytes, {room} left")
        self.needed, self.room = needed, room


def plan(
    op: OperationClass,
    limits: DemoLimits,
    ws_bytes: int,
    db_bytes: int,
    page_size: int,
    declared_bytes: int,
    query: dict[str, str],
) -> StoragePlan:
    """The bound of one request in a workspace of `ws_bytes` (its database: `db_bytes`)."""
    room = limits.max_workspace_bytes - ws_bytes
    files = {
        "none": 0,
        "upload": declared_bytes,
        "report": limits.max_report_bytes,
        "inference": limits.max_inference_bytes,
    }[op.files]
    if op.writes_db and files > room:
        raise InsufficientRoom(files, room)
    db_growth = max(0, room - files) if op.writes_db else 0
    db_cap = db_bytes + db_growth
    renders = op.pdf_render == "always" or (
        op.pdf_render == "bank_statement_upload" and query.get("kind") == "bank_statement"
    )
    return StoragePlan(
        op=op,
        db_cap=db_cap,
        db_growth=db_growth,
        journal=journal_bytes(db_cap, page_size) if op.writes_db else 0,
        files=files,
        render=pdf_render_bytes(limits) if renders else 0,
    )


def sqlite_page_size(db_path) -> int:
    """The page size from the database header (bytes 16-17; 1 means 65536)."""
    with open(db_path, "rb") as f:
        header = f.read(100)
    if len(header) < 18 or not header.startswith(b"SQLite format 3\x00"):
        raise ValueError("not a SQLite database")
    value = int.from_bytes(header[16:18], "big")
    return 65536 if value == 1 else value
