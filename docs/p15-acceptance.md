# P15 — Investigation Workflow, Cases and Evidence: Acceptance Record

Date: 2026-10-10 · Scope: [`roadmap.md`](roadmap.md) §P15, on top of P14b (`4d1e184`) ·
Design: [`investigations.md`](investigations.md).

**What P15 adds.**
- **Cases.** Investigations (`IV-`) inside an engagement case. The lifecycle (open / in review / awaiting
  evidence / concluded / closed) is documented, and invalid transitions are refused. Concluding, closing and
  re-opening need a reason. `inconclusive` is a valid outcome, and nothing closes automatically.
- **Evidence links.** 17 reference types. Each is scope-checked and pattern-validated, stored as a reference with a
  snapshot and its SHA-256, and resolved again on demand (current / changed / stale / missing). Unlinking is an
  append-only removal.
- **Notes and decisions.** Both are append-only. A decision records the subject, action, rationale, supporting
  and contradicting links, and an unresolved flag. Finding statuses New / Under review / Confirmed / Dismissed /
  Escalated are derived from the latest decision.
- **OCR corrections.** Explicit, append-only overlays that keep the original value. Before confirmation the
  overlay is applied in the extraction table. After confirmation it becomes one logged P4 transformation, which
  makes dependent reconciliation runs stale.
- **Audit.** Every write appends a P2 hash-chained audit entry and an event. There are a protected
  `GET /api/audit/verify`, a deterministic case summary and an evidence manifest that re-hashes files.
- **Interfaces.** Protected, paginated endpoints and an Investigations tab. "Open investigation" buttons appear
  on the P9 explanation and the P14 result detail.
- **Schema.** Migration 8 adds seven tables. Existing data is kept (tested 7 → 8).

**What it leaves alone.**
- P9 scores, detector thresholds, model predictions and existing findings are unchanged. The tests check the P9
  `result_sha256` and the signal set.
- P12 never merges on an analyst decision.
- P14 matching is unchanged. A decision on a result is an overlay, and the run hash is unchanged.
- Source rows, canonical rows and evidence files are never modified.
- No dependency was added.
- P16 is not started: there are no reports, so the report exclusion of dismissed findings is not built.

## Commands and results (final code)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .   # All checks passed; 246 files formatted
PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider -rs tests/backend
node tests/frontend/harness.js
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py
```

| Check | Result |
|---|---|
| Backend suite | 1139 passed, 0 failed, 0 skipped, 0 errors (exit 0; 1222 s; P14b baseline 1122 + 17 new) |
| P15 tests (`tests/backend/test_investigations.py`) | 17 passed |
| Node harness | 13 passed, 0 failed |
| Browser check (Playwright, Chromium) | 20 passed, 0 failed (incl. 2 A13 checks) |
| P4 integrity panel check | 6 passed, 0 failed |
| Ruff | check clean; format clean (246 files) |

**Earlier tests changed in P15:** none. No test was weakened, skipped or removed.

**Unconfirmed OCR rows are never matched.** P15 adds `reviewer_corrected` to the trusted review states only; the
P14b guard (`tests/backend/test_pdf_datasets.py::test_unconfirmed_extracted_lines_never_enter_matching`) still
refuses review-required lines, and the P15 test above shows a correction cannot bypass the review decision.

**Schema (migration 8).** `investigations` (status and texts change; no delete), `investigation_links`
(immutable), `investigation_link_removals`, `investigation_notes`, `investigation_decisions`,
`investigation_events` (with `audit_seq` → `audit_log`) and `ocr_corrections` (append-only). Migration 8 only
creates tables, indexes and triggers; it rewrites no existing row.

## Tests (`tests/backend/test_investigations.py`)

All tests run on real imports: the P5 synthetic ledger and masters, the P5 CSV bank statement, a PDF statement
derived from P5, and real reconciliation runs.

| Test | What it proves |
|---|---|
| `test_lifecycle_valid_and_invalid_transitions` | The path open → in review → awaiting evidence → in review → concluded → closed → in review (re-open) works, and the allowed transitions are reported. An invalid transition gives `invalid_transition` (409). Concluding needs an outcome and a conclusion (≥ 10 characters); closing and re-opening need a reason. A closed case refuses notes, decisions, edits and OCR corrections (`investigation_closed`). Each transition is an event with an increasing audit sequence, and the audit chain verifies. The other documented edges (open → awaiting evidence / closed, in review → open, concluded → in review) are allowed by the same table but not exercised one by one. |
| `test_create_validation_and_no_invented_identity` | Unknown case and empty title are refused. The actor is the session identity; no assignee is invented. |
| `test_every_link_type_resolves_within_scope` | All 17 link types resolve against real records, with a snapshot hash. Verification reports `current`. Linking the same record twice gives `duplicate_link` (409). |
| `test_invalid_cross_scope_and_hostile_references` | Malformed IDs, path traversal (`../`, `\`, control characters), unknown records (404), records from another engagement case (`cross_scope_reference`), wrong dataset kinds, unknown link types and an empty reason are all refused. Nothing is written for a refused request. |
| `test_unlink_removes_only_the_case_link` | Unlinking appends a removal with a reason; the referenced transaction remains, and the removed link stays in history. A second unlink and a decision on a removed link are refused. Re-linking creates a new link. Removals and links cannot be changed (triggers). |
| `test_notes_and_decisions_are_append_only_with_history` | Notes are stored verbatim (an HTML payload included). Actions that do not fit the subject and empty rationales are refused, and so is a supporting reference that is not a link of the case. The latest decision gives the finding status, and earlier decisions stay. UPDATE and DELETE are refused by trigger on links, removals, notes, decisions and events (each table holds rows, so the triggers really fire); an investigation cannot be deleted. |
| `test_ambiguity_resolution_needs_a_listed_candidate_and_leaves_the_run_unchanged` | `ambiguity_resolved` accepts only one of the result's competing candidates. The run, its statuses and `result_sha256` are unchanged. The decision appears in `analyst_decisions`. |
| `test_duplicate_and_entity_decisions_are_recorded_only` | Duplicate and entity decisions are recorded and shown. Nothing is deleted or merged. |
| `test_correction_before_confirmation_is_an_overlay` | An invalid value and an empty rationale are refused. The correction keeps the original value, page and source text; the stored extraction (`items_sha256`) and the PDF evidence hash are unchanged. A correction never makes an unconfirmed row trusted: a review-required row with a correction but no decision, or with an exclusion, stays out of the confirmed table; only a `confirm` decision lets it in (as `reviewer_corrected`). After confirmation the canonical row carries the corrected value and the `pdf_corrections` provenance with the original. |
| `test_correction_after_confirmation_invalidates_dependent_reconciliation` | A balance correction is refused (`not_correctable_after_confirmation`). A reference correction is one logged P4 transformation (`ocr_correction`) with an `ocr_row_corrected` decision and an `ocr.corrected` audit entry; the canonical row, the stored extraction and the PDF evidence are unchanged, and the audit chain verifies. The dependent run is reported as invalidated and listed `stale`; `get_run` refuses it even from cache (`inputs_changed`), and the link verifies as `stale`. A fresh run is not stale. The correction record keeps the original value and cannot be changed or deleted. The P4 revert removes the override, and the correction record remains. |
| `test_p9_detectors_and_source_rows_unchanged` | P9 `result_sha256` and the signal set are identical to the values before P15 actions, and the audit chain verifies. |
| `test_persistence_across_restart_and_deterministic_summary` | Cases, notes, summary and manifest hashes are identical after reopening the database. |
| `test_tampered_evidence_is_reported_in_the_manifest` | A modified evidence file behind a link is reported as `evidence_tampered`. |
| `test_pagination_on_100k_rows_is_correct_and_deterministic` | With 100,000 notes, the first and last pages are correct, the page after the last is empty, and order is deterministic. |
| `test_migration_7_to_8_keeps_existing_rows` | A version-7 database migrates to the current version with its rows kept, the seven P15 tables created and no foreign-key violations. |
| `test_investigation_endpoints_require_the_token` | The P15 list, read, create, link, note, decision, transition, OCR-correction, PATCH, audit-verify and both PDF correction endpoints refuse requests without the token (401); the GET endpoints also refuse a wrong token. |
| `test_investigation_api_flow_and_validation` | The end-to-end API flow works, and the summary is deterministic. Malformed path IDs, unknown body fields, `size` > 500, invalid transitions and cross-case links get explicit errors. Activity pages are ordered. Invalid PDF corrections (unknown item → `item_not_found`, a non-PDF dataset → `not_a_pdf_dataset`, an unknown field, an unknown body field, a malformed item ID) are refused and record nothing. |

**Browser (A13).**
- An investigation is opened from a reconciliation finding in the Bank Recon tab and links that finding.
- A note containing an HTML/script payload renders as text: no element is created and no script runs.
- Status and decision changes go through the real API, and only valid transitions are offered.
- The activity log lists created / linked / note_added / status_changed / decision_recorded, and
  `GET /api/audit/verify` is ok.

## Audit guarantees and limits

- Every P15 write runs in one transaction with an audit entry (`investigation.*`, `ocr.corrected`) and an event
  that references the entry's sequence number. Each records the actor (local OS identity) and a UTC timestamp.
- Links, removals, notes, decisions, events and corrections are append-only by SQLite trigger, and investigations
  cannot be deleted. `verify` checks only the audit log's own two triggers: a dropped or altered trigger on a P15
  table is **not** reported by `verify`.
- **Detects:** an edited, inserted, reordered or deleted entry within the chain.
- **Does not detect:** truncation of the newest entries, or a rewrite of the whole database by someone who
  controls the file and the code.
- **Status:** the log is **tamper-evident, not tamper-proof against an administrator**. Recording the head hash
  outside the database is the documented mitigation.

## Limitations

- Single local identity: no assignment, roles or second-person approval.
- Ambiguity resolutions and accepted entity links are recorded assessments. They do not feed back into P14
  matching or P12 merging.
- No run comparison, and no report exclusion of dismissed findings (P16).
- Rationales are length-checked, not verified as true.
- Snapshots are small summaries. Reproduction relies on the deterministic engines.
- After confirmation, balance, value-date and Dr/Cr fields cannot be corrected, and an amount correction cannot
  change direction.
- PDFs extracted before P15 must be re-extracted before a correction can be made before confirmation
  (`reextract_required`).
- Authorization is the session token: anyone holding it has full access.
- `GET /api/audit/verify` does not check the P15 tables' triggers (see above).
