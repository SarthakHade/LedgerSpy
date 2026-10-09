"""SQLite workspace database: connection handling and versioned schema migrations.

Migration 1 (P2): schema_version, audit_log, cases. Migration 2 (P3): evidence_files, datasets, transactions,
labels, ingest_issues. Migration 3 (P4): integrity runs/issues, fixes and decisions, transformations,
normalised values, plus immutability triggers on the P3 tables. Migration 4 (P11): the supervised model
registry (registered artefacts, status and activation events; all append-only). Migration 5 (P12): imported
master files and their records (vendor / customer / employee / group-company masters; immutable, raw values
only). Migration 6 (P14): datasets may be bank statements (`kind` 'bank_statement'; the table is rebuilt with
the same columns, rows and triggers because SQLite cannot alter a CHECK constraint) and the append-only
reconciliation run register. Later phases append migrations for their own tables;
existing migrations are never edited.

Migrations run with foreign-key enforcement switched off for the connection (the documented SQLite procedure
for rebuilding a referenced table) and `PRAGMA foreign_key_check` must be empty before the batch commits.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ledgerspy.cases import audit
from ledgerspy.config import LedgerSpyConfig


class DatabaseError(RuntimeError):
    pass


def _immutable(table: str, message: str) -> list[str]:
    return [
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{message}'); END",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{message}'); END",
    ]


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
    (
        3,
        "P4 quality: integrity runs and issues, fix proposals and decisions, transformations, "
        "normalised values",
        [
            # P3 rows become read-only for their canonical content: corrections go through logged
            # transformations into normalized_values. (Entity-ID columns stay writable for P12.)
            *_immutable("ingest_issues", "P3 ingestion issues are immutable"),
            *_immutable("labels", "labels are immutable"),
            "CREATE TRIGGER transactions_canonical_immutable "
            "BEFORE UPDATE OF dataset_id, txn_id, source_row, "
            "posted_at, document_date, amount_minor, currency, direction, account, payer_raw, payee_raw, "
            "reference, description, txn_type, extra ON transactions "
            "BEGIN SELECT RAISE(ABORT, "
            "'canonical transactions are immutable; use a logged transformation'); END",
            "CREATE TRIGGER transactions_no_delete BEFORE DELETE ON transactions "
            "BEGIN SELECT RAISE(ABORT, 'canonical transactions are never deleted'); END",
            """CREATE TABLE integrity_runs (
                run_id          TEXT PRIMARY KEY,
                dataset_id      TEXT NOT NULL REFERENCES datasets (dataset_id),
                created_at      TEXT NOT NULL,
                created_by      TEXT NOT NULL,
                config_sha256   TEXT NOT NULL,
                data_version    TEXT NOT NULL,
                report_sha256   TEXT NOT NULL,
                readiness       REAL NOT NULL,
                status          TEXT NOT NULL CHECK (status IN ('ready', 'ready_with_warnings', 'not_ready')),
                issue_count     INTEGER NOT NULL,
                report          TEXT NOT NULL
            )""",
            *_immutable("integrity_runs", "integrity runs are immutable"),
            """CREATE TABLE integrity_issues (
                run_id      TEXT NOT NULL REFERENCES integrity_runs (run_id),
                issue_id    TEXT NOT NULL,
                dataset_id  TEXT NOT NULL,
                check_code  TEXT NOT NULL,
                severity    TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error', 'critical')),
                field       TEXT,
                txn_id      TEXT,
                source_row  INTEGER,
                observed    TEXT NOT NULL,
                message     TEXT NOT NULL,
                fix_id      TEXT,
                PRIMARY KEY (run_id, issue_id)
            )""",
            "CREATE INDEX integrity_issues_by_check ON integrity_issues (run_id, check_code, severity)",
            "CREATE INDEX integrity_issues_by_txn ON integrity_issues (dataset_id, txn_id)",
            *_immutable("integrity_issues", "integrity issues are immutable"),
            # A proposed correction. Status: proposed -> accepted/rejected -> applied -> reverted.
            # Every decision is also appended to fix_decisions and the audit log.
            """CREATE TABLE fix_proposals (
                fix_id                TEXT PRIMARY KEY,
                dataset_id            TEXT NOT NULL REFERENCES datasets (dataset_id),
                txn_id                TEXT NOT NULL,
                source_row            INTEGER NOT NULL,
                field                 TEXT NOT NULL,
                transform             TEXT NOT NULL,
                original_value        TEXT NOT NULL,
                proposed_value        TEXT,
                candidates            TEXT NOT NULL,
                requires_confirmation INTEGER NOT NULL CHECK (requires_confirmation IN (0, 1)),
                reason                TEXT NOT NULL,
                status                TEXT NOT NULL
                    CHECK (status IN ('proposed', 'accepted', 'rejected', 'applied', 'reverted')),
                chosen_value          TEXT,
                first_run_id          TEXT NOT NULL,
                decided_by            TEXT,
                decided_at            TEXT,
                decision_reason       TEXT
            )""",
            """CREATE TABLE fix_decisions (
                decision_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                fix_id       TEXT NOT NULL REFERENCES fix_proposals (fix_id),
                dataset_id   TEXT NOT NULL,
                decision     TEXT NOT NULL CHECK (decision IN ('accept', 'reject')),
                chosen_value TEXT,
                reason       TEXT,
                decided_by   TEXT NOT NULL,
                decided_at   TEXT NOT NULL
            )""",
            *_immutable("fix_decisions", "fix decisions are append-only"),
            # Append-only log. kind='apply' changes a value; kind='revert' undoes one (reverts = its id).
            """CREATE TABLE transformations (
                transformation_id TEXT PRIMARY KEY,
                dataset_id        TEXT NOT NULL REFERENCES datasets (dataset_id),
                normalize_run_id  TEXT NOT NULL,
                kind              TEXT NOT NULL CHECK (kind IN ('apply', 'revert')),
                reverts           TEXT REFERENCES transformations (transformation_id),
                fix_id            TEXT NOT NULL REFERENCES fix_proposals (fix_id),
                txn_id            TEXT NOT NULL,
                source_row        INTEGER NOT NULL,
                field             TEXT NOT NULL,
                transform         TEXT NOT NULL,
                original_value    TEXT NOT NULL,
                normalized_value  TEXT NOT NULL,
                reason            TEXT NOT NULL,
                applied_by        TEXT NOT NULL,
                applied_at        TEXT NOT NULL
            )""",
            *_immutable("transformations", "the transformation log is append-only"),
            # The normalised view = canonical value, overridden by the latest applied transformation.
            """CREATE TABLE normalized_values (
                dataset_id        TEXT NOT NULL,
                txn_id            TEXT NOT NULL,
                field             TEXT NOT NULL,
                value             TEXT NOT NULL,
                transformation_id TEXT NOT NULL REFERENCES transformations (transformation_id),
                PRIMARY KEY (dataset_id, txn_id, field),
                FOREIGN KEY (dataset_id, txn_id) REFERENCES transactions (dataset_id, txn_id)
            )""",
        ],
    ),
    (
        4,
        "P11 supervised model registry: model_artifacts, model_status_events, model_activation_events",
        [
            # One row per registered artefact; the artefact file itself is verified against this hash on use.
            """CREATE TABLE model_artifacts (
                model_id        TEXT PRIMARY KEY
                                CHECK (model_id GLOB 'M-[0-9a-f]*' AND length(model_id) = 18),
                artifact_sha256 TEXT NOT NULL UNIQUE CHECK (length(artifact_sha256) = 64),
                model_type      TEXT NOT NULL
                                CHECK (model_type IN ('logistic_regression', 'gradient_boosting')),
                training_domain TEXT NOT NULL,
                label_source    TEXT NOT NULL,
                registered_by   TEXT NOT NULL,
                registered_at   TEXT NOT NULL
            )""",
            *_immutable("model_artifacts", "registered model artefacts are immutable"),
            # Lifecycle: the current status is the latest event. 'incompatible' is derived, never stored.
            """CREATE TABLE model_status_events (
                seq      INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id TEXT NOT NULL REFERENCES model_artifacts (model_id),
                status   TEXT NOT NULL CHECK (status IN ('experiment', 'candidate', 'approved', 'retired')),
                actor    TEXT NOT NULL,
                ts       TEXT NOT NULL,
                reason   TEXT NOT NULL,
                details  TEXT NOT NULL
            )""",
            *_immutable("model_status_events", "model status events are append-only"),
            # The active production model is the latest event's model_id (NULL: none).
            """CREATE TABLE model_activation_events (
                seq      INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id TEXT REFERENCES model_artifacts (model_id),
                actor    TEXT NOT NULL,
                ts       TEXT NOT NULL,
                reason   TEXT NOT NULL
            )""",
            *_immutable("model_activation_events", "model activation events are append-only"),
        ],
    ),
    (
        5,
        "P12 master data for entity resolution: master_files, master_records",
        [
            # An imported master file: evidence-backed, with the explicit field mapping used. Immutable.
            """CREATE TABLE master_files (
                master_file_id  TEXT PRIMARY KEY CHECK (master_file_id GLOB 'MF-[0-9a-f]*'),
                case_id         TEXT NOT NULL REFERENCES cases (case_id),
                evidence_sha256 TEXT NOT NULL REFERENCES evidence_files (sha256),
                original_filename TEXT NOT NULL,
                entity_type     TEXT NOT NULL
                                CHECK (entity_type IN ('vendor', 'customer', 'employee', 'group_company')),
                format          TEXT NOT NULL CHECK (format IN ('csv', 'xlsx')),
                mapping         TEXT NOT NULL,
                record_count    INTEGER NOT NULL CHECK (record_count >= 0),
                imported_by     TEXT NOT NULL,
                imported_at     TEXT NOT NULL
            )""",
            *_immutable("master_files", "imported master files are immutable"),
            # One row per data record: the raw text of the mapped columns only (never normalised here).
            """CREATE TABLE master_records (
                master_file_id TEXT NOT NULL REFERENCES master_files (master_file_id),
                source_row     INTEGER NOT NULL CHECK (source_row >= 1),
                record_id      TEXT,
                name           TEXT,
                address        TEXT,
                city           TEXT,
                phone          TEXT,
                email          TEXT,
                bank_account   TEXT,
                bank_code      TEXT,
                tax_id         TEXT,
                PRIMARY KEY (master_file_id, source_row)
            )""",
            *_immutable("master_records", "master records are immutable"),
        ],
    ),
    (
        6,
        "P14 bank statement reconciliation: bank_statement dataset kind, reconciliation_runs",
        [
            # Same columns, order and triggers as migration 2; only the kind CHECK gains 'bank_statement'.
            """CREATE TABLE datasets_v6 (
                dataset_id        TEXT PRIMARY KEY,
                case_id           TEXT NOT NULL REFERENCES cases (case_id),
                evidence_sha256   TEXT NOT NULL REFERENCES evidence_files (sha256),
                original_filename TEXT NOT NULL,
                kind              TEXT NOT NULL CHECK (kind IN ('ledger', 'bank_statement')),
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
            "INSERT INTO datasets_v6 SELECT * FROM datasets",
            "DROP TABLE datasets",
            "ALTER TABLE datasets_v6 RENAME TO datasets",
            "CREATE TRIGGER datasets_frozen BEFORE UPDATE ON datasets WHEN OLD.status = 'canonicalised' "
            "BEGIN SELECT RAISE(ABORT, 'a canonicalised dataset is immutable'); END",
            "CREATE TRIGGER datasets_no_delete BEFORE DELETE ON datasets "
            "BEGIN SELECT RAISE(ABORT, 'datasets are never deleted'); END",
            # One row per reconciliation run: its exact inputs (datasets + data versions, confirmed scopes and
            # direction semantics, configuration) and the hash of its result. Results are recomputed
            # deterministically from these inputs and verified against result_sha256. Append-only.
            """CREATE TABLE reconciliation_runs (
                run_id            TEXT PRIMARY KEY CHECK (run_id GLOB 'RR-[0-9a-f]*'),
                case_id           TEXT NOT NULL REFERENCES cases (case_id),
                ledger_dataset_id TEXT NOT NULL REFERENCES datasets (dataset_id),
                bank_dataset_id   TEXT NOT NULL REFERENCES datasets (dataset_id),
                parameters        TEXT NOT NULL,
                identity          TEXT NOT NULL,
                result_sha256     TEXT NOT NULL CHECK (length(result_sha256) = 64),
                summary           TEXT NOT NULL,
                created_by        TEXT NOT NULL,
                created_at        TEXT NOT NULL
            )""",
            *_immutable("reconciliation_runs", "reconciliation runs are append-only"),
        ],
    ),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def db_path_for(settings: LedgerSpyConfig) -> Path:
    return Path(settings.workspace.path) / settings.workspace.db_filename


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the database in autocommit mode; callers group writes with `transaction()`.

    check_same_thread=False: the API opens a connection per request in a FastAPI dependency, whose setup,
    endpoint and teardown may run on different worker threads. Each connection is still used by exactly one
    request at a time, never shared concurrently.
    """
    path = Path(path)
    if read_only:
        if not path.is_file():
            raise DatabaseError(f"database not found: {path}")
        conn = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None, check_same_thread=False
        )
    else:
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
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
    # Rebuilding a referenced table (migration 6) needs FK enforcement off; it is a no-op inside a
    # transaction, so it is set here and integrity is verified with foreign_key_check before the
    # batch commits.
    conn.execute("PRAGMA foreign_keys = OFF")
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
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise DatabaseError(
                    f"foreign key check failed after migration: {[tuple(r) for r in broken][:5]}"
                )
        return current_version(conn)
    finally:
        conn.close()
