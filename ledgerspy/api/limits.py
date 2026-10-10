"""Request bounds shared by every route (P20).

* `page` is bounded so `(page - 1) * size` always fits a SQLite integer (an unbounded page number made SQL
  OFFSET overflow and returned HTTP 500 before P20).
* Request bodies are bounded by `LocalSecurityMiddleware` (`MAX_BODY_BYTES`), except the file upload, which
  streams to disk under its own `ingest.max_file_bytes` limit.
"""

from typing import Annotated

from fastapi import Query

PAGE_MAX = 100_000_000  # × the largest page size (5000) stays far below 2**63
MAX_BODY_BYTES = 8 * 1024 * 1024  # the largest legitimate JSON body (5000 P4 decisions) is about 3 MB

Page = Annotated[int, Query(ge=1, le=PAGE_MAX)]
