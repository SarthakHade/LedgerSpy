"""Case persistence (skeleton). Full case management is P15."""

import sqlite3
from datetime import date
from typing import Any

from ledgerspy.canonical.ids import new_case_id
from ledgerspy.cases import audit
from ledgerspy.cases.db import transaction
from ledgerspy.hashing import utc_now_iso

_CASE_COLUMNS = "case_id, name, client, period_start, period_end, status, created_by, created_at"


def create_case(
    conn: sqlite3.Connection,
    *,
    name: str,
    actor: str,
    client: str | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> dict[str, Any]:
    case = {
        "case_id": new_case_id(),
        "name": name,
        "client": client,
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
        "status": "open",
        "created_by": actor,
        "created_at": utc_now_iso(),
    }
    with transaction(conn):
        conn.execute(
            f"INSERT INTO cases ({_CASE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",  # noqa: S608 — constant
            tuple(case.values()),
        )
        audit.append(conn, actor, "case.created", f"case:{case['case_id']}", case)
    return case


def get_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {_CASE_COLUMNS} FROM cases WHERE case_id = ?", (case_id,)).fetchone()  # noqa: S608
    return dict(row) if row else None
