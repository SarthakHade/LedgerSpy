"""SQLite workspace database: connection handling and versioned schema migrations.

Migration 1 (P2): schema_version, audit_log, cases. Migration 2 (P3): evidence_files, datasets, transactions,
labels, ingest_issues. Migration 3 (P4): integrity runs/issues, fixes and decisions, transformations,
normalised values, plus immutability triggers on the P3 tables. Migration 4 (P11): the supervised model
registry (registered artefacts, status and activation events; all append-only). Migration 5 (P12): imported
master files and their records (vendor / customer / employee / group-company masters; immutable, raw values
only). Migration 6 (P14): datasets may be bank statements (`kind` 'bank_statement'; the table is rebuilt with
the same columns, rows and triggers because SQLite cannot alter a CHECK constraint) and the append-only
reconciliation run register. Migration 7 (P14b): datasets may be PDF statements (`format` 'pdf'; rebuilt
the same way), and PDF extractions, their items and review decisions (all append-only). Migration 8 (P15):
investigation cases inside an engagement case, their evidence links (immutable, removals append-only), notes,
decisions and events (append-only), and OCR correction overlays (append-only). Migration 9 (P16): the
append-only register of generated report artefacts. Later phases append
migrations for their own tables; existing migrations are never edited.

Migrations run with foreign-key enforcement switched off for the connection (the documented SQLite procedure
for rebuilding a referenced table) and `PRAGMA foreign_key_check` must be empty before the batch commits.
"""

import functools
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
    (
        7,
        "P14b PDF bank statements: pdf dataset format, pdf_extractions, pdf_extraction_items, "
        "pdf_review_decisions",
        [
            # Same columns, order and triggers as migration 6; only the format CHECK gains 'pdf'.
            """CREATE TABLE datasets_v7 (
                dataset_id        TEXT PRIMARY KEY,
                case_id           TEXT NOT NULL REFERENCES cases (case_id),
                evidence_sha256   TEXT NOT NULL REFERENCES evidence_files (sha256),
                original_filename TEXT NOT NULL,
                kind              TEXT NOT NULL CHECK (kind IN ('ledger', 'bank_statement')),
                format            TEXT NOT NULL CHECK (format IN ('csv', 'xlsx', 'pdf')),
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
            "INSERT INTO datasets_v7 SELECT * FROM datasets",
            "DROP TABLE datasets",
            "ALTER TABLE datasets_v7 RENAME TO datasets",
            "CREATE TRIGGER datasets_frozen BEFORE UPDATE ON datasets WHEN OLD.status = 'canonicalised' "
            "BEGIN SELECT RAISE(ABORT, 'a canonicalised dataset is immutable'); END",
            "CREATE TRIGGER datasets_no_delete BEFORE DELETE ON datasets "
            "BEGIN SELECT RAISE(ABORT, 'datasets are never deleted'); END",
            # One extraction run of a PDF dataset's evidence: method per page, engine versions, configuration,
            # coverage and statement checks. Identified by evidence + configuration + engine; never changed.
            """CREATE TABLE pdf_extractions (
                extraction_id   TEXT PRIMARY KEY CHECK (extraction_id GLOB 'PE-[0-9a-f]*'),
                dataset_id      TEXT NOT NULL REFERENCES datasets (dataset_id),
                evidence_sha256 TEXT NOT NULL REFERENCES evidence_files (sha256),
                config_sha256   TEXT NOT NULL,
                status          TEXT NOT NULL CHECK (status IN ('completed', 'incomplete', 'failed')),
                engine          TEXT NOT NULL,
                report          TEXT NOT NULL,
                items_sha256    TEXT,
                error           TEXT,
                created_by      TEXT NOT NULL,
                created_at      TEXT NOT NULL
            )""",
            *_immutable("pdf_extractions", "PDF extractions are immutable"),
            # Every line of every page, classified, with page, box, source text, cells and confidences.
            """CREATE TABLE pdf_extraction_items (
                extraction_id   TEXT NOT NULL REFERENCES pdf_extractions (extraction_id),
                item_no         INTEGER NOT NULL CHECK (item_no >= 1),
                item_id         TEXT NOT NULL CHECK (item_id GLOB 'PX-[0-9a-f]*'),
                page            INTEGER NOT NULL CHECK (page >= 1),
                kind            TEXT NOT NULL,
                method          TEXT NOT NULL,
                review_required INTEGER NOT NULL CHECK (review_required IN (0, 1)),
                item            TEXT NOT NULL,
                PRIMARY KEY (extraction_id, item_no),
                UNIQUE (extraction_id, item_id)
            )""",
            *_immutable("pdf_extraction_items", "PDF extraction items are immutable"),
            # Reviewer decisions, append-only: the latest decision per item is in force until the dataset is
            # confirmed.
            """CREATE TABLE pdf_review_decisions (
                seq             INTEGER PRIMARY KEY AUTOINCREMENT,
                extraction_id   TEXT NOT NULL REFERENCES pdf_extractions (extraction_id),
                item_id         TEXT NOT NULL,
                decision        TEXT NOT NULL CHECK (decision IN ('confirm', 'exclude')),
                reason          TEXT,
                decided_by      TEXT NOT NULL,
                decided_at      TEXT NOT NULL
            )""",
            *_immutable("pdf_review_decisions", "review decisions are append-only"),
        ],
    ),
    (
        8,
        "P15 investigation workflow: investigations, links, link removals, notes, decisions, events, "
        "OCR corrections",
        [
            # An investigation case inside an engagement case (`cases`, the scope of its evidence). Status,
            # priority and texts change only through the service, each change audit-logged and recorded in
            # investigation_events; rows are never deleted.
            """CREATE TABLE investigations (
                investigation_id  TEXT PRIMARY KEY CHECK (investigation_id GLOB 'IV-*'),
                case_id           TEXT NOT NULL REFERENCES cases (case_id),
                title             TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
                description       TEXT NOT NULL,
                priority          TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'critical')),
                status            TEXT NOT NULL
                    CHECK (status IN ('open', 'in_review', 'awaiting_evidence', 'concluded', 'closed')),
                outcome           TEXT CHECK (outcome IN ('concerns_substantiated', 'no_issue_identified',
                                                           'inconclusive', 'referred')),
                conclusion        TEXT,
                closure_rationale TEXT,
                created_by        TEXT NOT NULL,
                created_at        TEXT NOT NULL,
                updated_by        TEXT NOT NULL,
                updated_at        TEXT NOT NULL
            )""",
            "CREATE TRIGGER investigations_no_delete BEFORE DELETE ON investigations "
            "BEGIN SELECT RAISE(ABORT, 'investigations are never deleted'); END",
            "CREATE INDEX investigations_by_case ON investigations (case_id, created_at)",
            # A reference to an existing record (never a copy of it): type, dataset / run, record ID, a small
            # snapshot of what was linked and the SHA-256 of the full referenced object at link time.
            """CREATE TABLE investigation_links (
                link_id          TEXT PRIMARY KEY CHECK (link_id GLOB 'LK-*'),
                investigation_id TEXT NOT NULL REFERENCES investigations (investigation_id),
                link_type        TEXT NOT NULL,
                dataset_id       TEXT REFERENCES datasets (dataset_id),
                run_id           TEXT REFERENCES reconciliation_runs (run_id),
                ref_id           TEXT NOT NULL,
                scope_key        TEXT NOT NULL,
                snapshot         TEXT NOT NULL,
                snapshot_sha256  TEXT NOT NULL CHECK (length(snapshot_sha256) = 64),
                reason           TEXT NOT NULL CHECK (length(reason) >= 3),
                annotation       TEXT,
                created_by       TEXT NOT NULL,
                created_at       TEXT NOT NULL
            )""",
            *_immutable("investigation_links", "investigation links are immutable"),
            "CREATE INDEX investigation_links_by_inv ON investigation_links (investigation_id, created_at)",
            # Unlinking removes only the case link (never the evidence); a removed link stays in history.
            """CREATE TABLE investigation_link_removals (
                link_id     TEXT PRIMARY KEY REFERENCES investigation_links (link_id),
                reason      TEXT NOT NULL CHECK (length(reason) >= 3),
                removed_by  TEXT NOT NULL,
                removed_at  TEXT NOT NULL
            )""",
            *_immutable("investigation_link_removals", "link removals are append-only"),
            """CREATE TABLE investigation_notes (
                note_id          TEXT PRIMARY KEY CHECK (note_id GLOB 'NT-*'),
                investigation_id TEXT NOT NULL REFERENCES investigations (investigation_id),
                link_id          TEXT REFERENCES investigation_links (link_id),
                body             TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 10000),
                created_by       TEXT NOT NULL,
                created_at       TEXT NOT NULL
            )""",
            *_immutable("investigation_notes", "investigation notes are append-only"),
            # An analyst decision on the investigation or on one linked subject. The latest decision on a
            # subject is its current assessment; earlier ones stay. Automated outputs are never changed by
            # a decision.
            """CREATE TABLE investigation_decisions (
                decision_id      TEXT PRIMARY KEY CHECK (decision_id GLOB 'DC-*'),
                investigation_id TEXT NOT NULL REFERENCES investigations (investigation_id),
                link_id          TEXT REFERENCES investigation_links (link_id),
                action           TEXT NOT NULL,
                rationale        TEXT NOT NULL CHECK (length(rationale) >= 3),
                payload          TEXT NOT NULL,
                supporting       TEXT NOT NULL,
                contradicting    TEXT NOT NULL,
                unresolved       INTEGER NOT NULL CHECK (unresolved IN (0, 1)),
                created_by       TEXT NOT NULL,
                created_at       TEXT NOT NULL
            )""",
            *_immutable("investigation_decisions", "investigation decisions are append-only"),
            # Every meaningful action on an investigation, in order, with the audit-log entry that records it.
            """CREATE TABLE investigation_events (
                seq              INTEGER PRIMARY KEY AUTOINCREMENT,
                investigation_id TEXT NOT NULL REFERENCES investigations (investigation_id),
                event            TEXT NOT NULL,
                from_status      TEXT,
                to_status        TEXT,
                subject_id       TEXT,
                details          TEXT NOT NULL,
                audit_seq        INTEGER NOT NULL REFERENCES audit_log (seq),
                actor            TEXT NOT NULL,
                ts               TEXT NOT NULL
            )""",
            *_immutable("investigation_events", "investigation events are append-only"),
            "CREATE INDEX investigation_events_by_inv ON investigation_events (investigation_id, seq)",
            # OCR (PDF extraction) field corrections: an overlay, never a change of the PDF or the extraction.
            # Before confirmation the extracted table uses the latest correction; after confirmation it is
            # applied as one logged P4 transformation (transformation_id).
            """CREATE TABLE ocr_corrections (
                correction_id    TEXT PRIMARY KEY CHECK (correction_id GLOB 'OC-*'),
                dataset_id       TEXT NOT NULL REFERENCES datasets (dataset_id),
                extraction_id    TEXT NOT NULL REFERENCES pdf_extractions (extraction_id),
                item_id          TEXT NOT NULL,
                field            TEXT NOT NULL,
                original_value   TEXT,
                corrected_value  TEXT NOT NULL,
                rationale        TEXT NOT NULL CHECK (length(rationale) >= 3),
                page             INTEGER NOT NULL,
                source_text      TEXT NOT NULL,
                stage            TEXT NOT NULL CHECK (stage IN ('before_confirmation', 'after_confirmation')),
                transformation_id TEXT REFERENCES transformations (transformation_id),
                investigation_id TEXT REFERENCES investigations (investigation_id),
                created_by       TEXT NOT NULL,
                created_at       TEXT NOT NULL
            )""",
            *_immutable("ocr_corrections", "OCR corrections are append-only"),
        ],
    ),
    (
        9,
        "P16 audit reporting: reports (append-only register of generated report artefacts)",
        [
            # One row per generated artefact set. Regenerating a report creates a new version (a new row and
            # new files); earlier versions are never overwritten. The files live in <workspace>/reports/ under
            # names derived from report_id only, read-only, with their SHA-256 recorded here.
            """CREATE TABLE reports (
                report_id          TEXT PRIMARY KEY CHECK (report_id GLOB 'RP-*'),
                report_type        TEXT NOT NULL CHECK (report_type IN ('case_investigation',
                                   'bank_reconciliation', 'audit_memo', 'evidence_manifest')),
                subject_key        TEXT NOT NULL,
                case_id            TEXT REFERENCES cases (case_id),
                version            INTEGER NOT NULL CHECK (version >= 1),
                status             TEXT NOT NULL CHECK (status IN ('generated', 'draft')),
                params             TEXT NOT NULL,
                provenance         TEXT NOT NULL,
                content_sha256     TEXT NOT NULL CHECK (length(content_sha256) = 64),
                artifacts          TEXT NOT NULL,
                schema_version     INTEGER NOT NULL,
                format_version     INTEGER NOT NULL,
                config_sha256      TEXT NOT NULL,
                previous_report_id TEXT REFERENCES reports (report_id),
                generated_by       TEXT NOT NULL,
                generated_at       TEXT NOT NULL,
                UNIQUE (report_type, subject_key, version)
            )""",
            *_immutable("reports", "generated reports are append-only"),
            "CREATE INDEX reports_by_case ON reports (case_id, generated_at)",
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


@functools.lru_cache(maxsize=1)
def expected_triggers() -> dict[str, str]:
    """Every trigger the migrations leave behind, with its exact SQL (replayed on an in-memory database)."""
    mem = sqlite3.connect(":memory:")
    try:
        for _version, _desc, statements in MIGRATIONS:
            for statement in statements:
                mem.execute(statement)
        return dict(mem.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"))
    finally:
        mem.close()


def check_triggers(conn: sqlite3.Connection) -> dict:
    """P20: compare the database's triggers with the ones its migrations create. A dropped or altered
    append-only / immutability trigger (or an extra one) is reported. This detects a weakened schema; it
    cannot detect an edit made while a trigger was dropped and then restored exactly."""
    found = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"))
    expected = expected_triggers()
    missing = sorted(set(expected) - set(found))
    altered = sorted(n for n in set(expected) & set(found) if expected[n] != found[n])
    unexpected = sorted(set(found) - set(expected))
    return {
        "ok": not (missing or altered or unexpected),
        "expected": len(expected),
        "missing": missing,
        "altered": altered,
        "unexpected": unexpected,
    }


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
