"""SQLite workspace database: connection handling and versioned schema migrations.

Only the P2 foundation tables exist here (schema_version, audit_log, cases). Later phases append migrations
for their own tables (evidence_files and datasets in P3, runs/signals/scores in P9, …); existing migrations
are never edited.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ledgerspy.cases import audit
from ledgerspy.config import LedgerSpyConfig


class DatabaseError(RuntimeError):
    pass


# (version, description, statements). Statements run inside one transaction per migration batch.
MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "P2 foundation: schema_version, audit_log, cases",
        [
            """CREATE TABLE schema_version (
                version     INTEGER PRIMARY KEY,
                description TEXT NOT NULL
            )""",
            *audit.DDL,
            """CREATE TABLE cases (
                case_id      TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                client       TEXT,
                period_start TEXT,
                period_end   TEXT,
                status       TEXT NOT NULL CHECK (status IN ('open', 'in_review', 'closed')),
                created_by   TEXT NOT NULL,
                created_at   TEXT NOT NULL
            )""",
        ],
    ),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def db_path_for(settings: LedgerSpyConfig) -> Path:
    return Path(settings.workspace.path) / settings.workspace.db_filename


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the database in autocommit mode; callers group writes with `transaction()`."""
    path = Path(path)
    if read_only:
        if not path.is_file():
            raise DatabaseError(f"database not found: {path}")
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    else:
        conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE … COMMIT, or ROLLBACK on error. Serialises writers (needed for the audit chain)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def init_db(path: Path | str, actor: str) -> int:
    """Create or upgrade the database to SCHEMA_VERSION. Idempotent. Returns the schema version."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        with transaction(conn):
            version = current_version(conn)
            if version > SCHEMA_VERSION:
                raise DatabaseError(
                    f"database schema v{version} is newer than this LedgerSpy (v{SCHEMA_VERSION})"
                )
            for mig_version, description, statements in MIGRATIONS:
                if mig_version <= version:
                    continue
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                    (mig_version, description),
                )
                audit.append(
                    conn,
                    actor,
                    "schema.migrated",
                    f"schema:{mig_version}",
                    {"version": mig_version, "description": description},
                )
        return current_version(conn)
    finally:
        conn.close()
