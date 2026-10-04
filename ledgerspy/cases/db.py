"""SQLite workspace database: connection handling and versioned schema migrations.

Migration 1 (P2): schema_version, audit_log, cases. Migration 2 (P3): evidence_files, datasets, transactions,
labels, ingest_issues. Later phases append migrations for their own tables (runs/signals/scores in P9, …);
existing migrations are never edited.
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
    (
        2,
        "P3 ingestion: evidence_files, datasets, transactions, labels, ingest_issues",
        [
            # Content-addressed evidence. Rows are immutable; the file itself is stored read-only.
            """CREATE TABLE evidence_files (
                sha256      TEXT PRIMARY KEY CHECK (length(sha256) = 64),
                size_bytes  INTEGER NOT NULL CHECK (size_bytes > 0),
                stored_name TEXT NOT NULL UNIQUE,
                stored_at   TEXT NOT NULL
            )""",
            "CREATE TRIGGER evidence_files_no_update BEFORE UPDATE ON evidence_files "
            "BEGIN SELECT RAISE(ABORT, 'evidence_files is immutable'); END",
            "CREATE TRIGGER evidence_files_no_delete BEFORE DELETE ON evidence_files "
            "BEGIN SELECT RAISE(ABORT, 'evidence_files is immutable'); END",
            # One upload of one file into one case. The mapping columns are filled once, on confirmation.
            """CREATE TABLE datasets (
                dataset_id        TEXT PRIMARY KEY,
                case_id           TEXT NOT NULL REFERENCES cases (case_id),
                evidence_sha256   TEXT NOT NULL REFERENCES evidence_files (sha256),
                original_filename TEXT NOT NULL,
                kind              TEXT NOT NULL CHECK (kind IN ('ledger')),
                format            TEXT NOT NULL CHECK (format IN ('csv', 'xlsx')),
                format_details    TEXT NOT NULL,
                header            TEXT NOT NULL,
                row_count         INTEGER NOT NULL,
                profile           TEXT NOT NULL,
                suggestion        TEXT NOT NULL,
                upload_config_sha256 TEXT NOT NULL,
                status            TEXT NOT NULL CHECK (status IN ('mapping_required', 'canonicalised')),
                created_by        TEXT NOT NULL,
                created_at        TEXT NOT NULL,
                mapping           TEXT,
                mapping_sha256    TEXT,
                adapter           TEXT,
                adapter_version   TEXT,
                config_sha256     TEXT,
                time_basis        TEXT CHECK (time_basis IN ('date', 'datetime', 'simulation_step')),
                synthetic         INTEGER CHECK (synthetic IN (0, 1)),
                id_source         TEXT CHECK (id_source IN ('source_column', 'derived')),
                issue_summary     TEXT,
                canonicalised_by  TEXT,
                canonicalised_at  TEXT
            )""",
            "CREATE TRIGGER datasets_frozen BEFORE UPDATE ON datasets WHEN OLD.status = 'canonicalised' "
            "BEGIN SELECT RAISE(ABORT, 'a canonicalised dataset is immutable'); END",
            "CREATE TRIGGER datasets_no_delete BEFORE DELETE ON datasets "
            "BEGIN SELECT RAISE(ABORT, 'datasets are never deleted'); END",
            # Canonical transactions (ledgerspy.canonical.Transaction). No label column exists here.
            """CREATE TABLE transactions (
                dataset_id      TEXT NOT NULL REFERENCES datasets (dataset_id),
                txn_id          TEXT NOT NULL,
                source_row      INTEGER NOT NULL CHECK (source_row >= 1),
                posted_at       TEXT,
                document_date   TEXT,
                amount_minor    INTEGER,
                currency        TEXT,
                direction       TEXT CHECK (direction IN ('debit', 'credit', 'in', 'out')),
                account         TEXT,
                payer_raw       TEXT,
                payee_raw       TEXT,
                payer_entity_id TEXT,
                payee_entity_id TEXT,
                reference       TEXT,
                description     TEXT,
                txn_type        TEXT,
                extra           TEXT NOT NULL,
                PRIMARY KEY (dataset_id, txn_id),
                UNIQUE (dataset_id, source_row)
            )""",
            # Ground truth, kept apart from transactions: evaluation only (P10), never features or scoring.
            """CREATE TABLE labels (
                dataset_id  TEXT NOT NULL,
                txn_id      TEXT NOT NULL,
                column_name TEXT NOT NULL,
                value       TEXT NOT NULL,
                PRIMARY KEY (dataset_id, txn_id, column_name),
                FOREIGN KEY (dataset_id, txn_id) REFERENCES transactions (dataset_id, txn_id)
            )""",
            # Every mapped cell that could not be converted (the transaction field is NULL instead).
            """CREATE TABLE ingest_issues (
                dataset_id    TEXT NOT NULL REFERENCES datasets (dataset_id),
                source_row    INTEGER NOT NULL,
                field         TEXT NOT NULL,
                source_column TEXT,
                code          TEXT NOT NULL,
                raw_value     TEXT,
                PRIMARY KEY (dataset_id, source_row, field, code)
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
