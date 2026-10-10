# P16 — Audit Reporting and Evidence-Backed Memos: Acceptance Record

Date: 2026-10-11 · Scope: [`roadmap.md`](roadmap.md) §P16, on top of P15 (`6b1d9f3`) ·
Methodology: [`reporting.md`](reporting.md).

**What P16 adds.**
- **Four report types:**
  - **case investigation report:** one investigation;
  - **bank reconciliation report:** one reconciliation run;
  - **draft audit memo:** 1–20 investigations of one engagement case;
  - **evidence manifest:** the same selection.

  All are built read-only from P15 investigations, the P14 run register and run results, P14b extraction rows,
  and the P2 evidence store. There is no second analytics or reconciliation engine.
- **Claims:** every material statement records its origin (automated / analyst / computed), its records with
  verification state and source location (file, row, PDF page and row), its evidence files with SHA-256 and
  re-verification, its limitations, and contradicting evidence. Traceability is `traceable` / `unverified` /
  `no_reference` and is defined as "where it comes from", never as "true".
- **Totals:**
  - kept per dataset / account and currency;
  - rebuilt from row-level results and checked against the run's own totals;
  - grouped matches and doubly linked transactions count once.
- **Stale and unconfirmed data:**
  - a stale run is reported as superseded, never as current;
  - unconfirmed extraction values never enter totals and are flagged.
- **Memo:**
  - eleven fixed sections from deterministic templates, always a draft;
  - every number is cross-checked against the report model;
  - dismissed findings are listed separately and not cited as material;
  - recommendations are labelled as recommendations.
- **Artefacts:**
  - JSON, escaped self-contained HTML (no-script CSP) and editable Markdown;
  - written exclusively and read-only, never overwritten, versioned;
  - recorded with their hashes in an append-only register (migration 9) plus an audit entry;
  - re-hashed on every read.
- **API and UI:** seven protected endpoints and a Reports tab (preview, generate, list, open, verify,
  download).

**What it leaves alone.**
- **P9 and detectors:** P9 scores, detector thresholds, model predictions and existing findings are unchanged.
  - **Hashes:** no configuration section was added, so the configuration hash, and with it every P9 identity,
    is the same as at P15. The baseline and after values of `load_config().sha256` and the feature-registry
    hash are identical.
  - **Code:** there is no diff under `config/`, `ledgerspy/risk`, `features`, `detectors`, `anomaly`,
    `forensics` or `supervised`.
  - **Tests:** a test asserts the P9 `result_sha256` before and after generating every report type.
- **Sources:** a report writes nothing except its register row, its files and one audit entry (enforced in a
  test with a SQLite authorizer).
- **No new dependencies;** no network; no language model.
- **P17 is not started.**

**One change to P15 code.** `investigations.refs` gains `check()`, which returns the record's current snapshot
next to the verification state that `verify()` already returned. `verify()` now delegates to it, and its output
is unchanged (the P15 tests pass unmodified).

## Commands and results (final code)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .
PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider -rs tests/backend
node tests/frontend/harness.js
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py
```

| Check | Result |
|---|---|
| Backend suite | 1160 passed, 0 failed, 0 skipped, 0 errors (exit 0; 1089 s; P15 baseline 1139 + 21 new) |
| P16 tests (`tests/backend/test_reports.py`) | 21 passed (in the run above; the OCR test ran, Tesseract present) |
| Node harness | 13 passed, 0 failed |
| Browser check (Playwright, Chromium) | 22 passed, 0 failed (incl. 2 new A14 checks) |
| P4 integrity panel check | 6 passed, 0 failed |
| Ruff | check clean; format clean (256 files) |

**Earlier tests changed in P16:** none in the backend suite. In `browser_check.py`, A14 was added and the
Reports tab joins A7's every-tab height check. No test was weakened, skipped or removed.

## Tests (`tests/backend/test_reports.py`)

The tests use the shared P15 fixture: the P5 synthetic ledger and masters, the P5 CSV statement, P5-derived PDF
statements and a real reconciliation run. The investigations are created through the P15 service. Only the
golden-template and one totals unit test use hand-written, in-memory data.

| Test | What it proves |
|---|---|
| `test_case_report_separates_findings_assessments_notes_and_conclusion` | **Separation:** automated findings, analyst assessments and notes carry their own origins; the P9 text says "not a probability"; a dismissed finding is kept with its status. **Traceability:** a traceable finding resolves to `current` with the ledger's evidence SHA-256 verified and its source row. **Contradiction:** a decision's contradicting link is shown. **Case level:** a case-level decision is `no_reference` and listed as open. **Conclusion and activity:** no conclusion is invented; the activity carries audit hashes. |
| `test_linked_values_count_each_transaction_once_and_trace_to_rows` | A transaction linked as a transaction and as a P9 risk row counts once; the total equals the canonical row's amount and is labelled "not a loss". |
| `test_conclusion_only_when_recorded_and_missing_data_is_stated` | A recorded `inconclusive` conclusion is reproduced as analyst-originated. An empty investigation gives empty sections, and the HTML says "No conclusion recorded" and "No transaction is linked directly". |
| `test_reconciliation_report_totals_reconcile_with_rows_and_keep_accounts_apart` | **Rows:** the account × currency × status breakdown adds up to the run's row counts and per-currency totals, and its rows are exactly the run's distinct row IDs. **Accounts:** the two statement accounts are kept apart. **Exceptions:** they are exactly the non-matched results, bounded and stated. |
| `test_currencies_and_grouped_matches_are_never_merged_or_double_counted` | **Isolated unit test (in-memory run).** A grouped match of two ledger rows counts each row once. Two currencies and an unknown currency stay in separate buckets, and the unknown one is labelled "(currency unknown)". A breakdown that does not match the run's totals refuses the report (`totals_mismatch`). |
| `test_stale_run_is_never_presented_as_current` | **Setup:** a genuine after-confirmation OCR correction makes a run stale. **Report:** `run_state: stale`, no totals, exceptions or results; the HTML is labelled STALE. **Links:** the case report marks the link `stale`, its claim `unverified` and the run stale. **Memo:** it recommends a new run. |
| `test_unconfirmed_ocr_values_are_not_presented_as_facts` | **Requires local Tesseract; skipped elsewhere.** A review-required OCR row from a scanned page is an `extraction_value` labelled "not confirmed financial facts", never enters a total, and is listed under data quality. |
| `test_memo_is_a_draft_with_every_number_traced_and_all_sections` | **Status:** `draft`, approval `not_reviewed`, unsupported. **Content:** the eleven sections in order; facts equal counts recomputed from the case reports; dismissed findings are not material but are listed; recommendations are labelled; contradicting evidence appears. **Number check:** an invented number makes it fail (`memo_number_unsupported`). |
| `test_memo_templates_contain_no_hardcoded_figures` | No string literal in the memo builder contains a figure outside its placeholders. |
| `test_memo_refuses_investigations_from_different_engagement_cases` | Investigations from two engagement cases are refused (`cross_scope_reference`); unknown investigations give 404. |
| `test_evidence_manifest_lists_hashes_locations_and_integrity` | **Evidence:** files carry SHA-256, original names and verification. **References:** every link is listed with its source row and evidence. **Counts:** they match the listings. |
| `test_deterministic_content_and_versioned_regeneration` | **Determinism:** identical previews and content hash. **Versioning:** two generations give versions 1 and 2 with distinct IDs, `previous_report_id` and `same_content_as_previous`, and both sets stay on disk, read-only, with matching hashes. **Register:** it refuses UPDATE and DELETE. **Audit:** an audit entry is written, and the chain verifies. |
| `test_reports_never_write_sources_or_read_ground_truth` | **Guard:** every report type is previewed and generated under a SQLite authorizer that denies reading `labels` and writing outside `reports` / `audit_log`. Nothing was denied, and positive controls show the guard bites. **Sources:** the source tables' digest and row counts are unchanged. **P9:** `result_sha256` is equal before and after (and to the fixture's). **Code:** the report source contains no `labels`, ground-truth, scenario or `SYNTHETIC_` references. |
| `test_html_and_markdown_escape_hostile_text` | **Inputs:** a `<script>` note and an `<img onerror>` title. **HTML:** both are escaped, there is no `<script`, and the CSP is present. **Markdown:** HTML-escaped, with links, emphasis and code neutralised. **JSON:** keeps the record verbatim. |
| `test_tampered_or_missing_artifacts_are_refused` | An edited file gives `report_tampered` and a deleted file gives `report_missing`; `verify` reports each format. |
| `test_oversized_output_is_refused_and_leaves_nothing` | Over the size limit: `report_too_large` (413), with no register row and no file. |
| `test_parameters_are_validated` | Unknown types, wrong parameter combinations, unknown runs or investigations, empty and duplicate selections, and a path-like report ID are each refused with an explicit code. |
| `test_migration_8_to_9_keeps_existing_rows` | A v8 database migrates with its rows; the `reports` table, triggers and index exist; there are no foreign-key violations. |
| `test_report_endpoints_require_the_token` | Every report endpoint gives 401 without a token or with a wrong one. |
| `test_report_api_flow_validation_and_downloads` | **Flow:** the vocabulary (approval unsupported, PDF deferred), preview equals stored content, the memo stored as a draft, and list pagination. **Downloads:** content types, safe attachment names, `X-Content-SHA256` equal to the body and the register, and CSP headers. **Refusals:** unknown body fields, path traversal in IDs and formats, `format=pdf`, more than 20 investigations, malformed IDs, `size` > 500. |
| `test_memo_templates_match_the_golden_files` | The Markdown and HTML templates reproduce `tests/backend/fixtures/reports/memo.{md,html}` byte for byte (hand-written content). |

**Browser (A14).**
- **Case-report preview:** the Reports tab's preview shows the API's content hash, the automated-findings and
  analyst-assessment sections apart, and the A13 hostile note as text: no `<img>` element and no script.
- **Stored memos:** two generations of a draft memo give versions 2 and 1, listed in the tab.
- **Download:** the download, through the UI's token-bearing blob request, hashes to the recorded SHA-256. It
  contains the DRAFT banner and the escaped note, with a CSP that allows nothing.

**Export schema check (manual, not part of the suite).** One report of each type was generated from the test
data and checked against the schema in [`reporting.md`](reporting.md) §6:
- **JSON:** the top level is exactly `{envelope, content}`; the envelope has its 15 documented keys; the
  content's keys, provenance, claim and reference fields are as documented; `content_sha256` was recomputed
  independently from the canonical JSON and matched.
- **HTML:** parsed, it contains no script, img, iframe, object, embed, form, a, link or base element.
- **Markdown:** it starts with a heading and contains no raw `<`.

**Manual check.** In the running app (preview server), a case report was previewed, generated twice (versions
1 and 2, same content hash), verified and downloaded; a memo for the same investigation was previewed. Sample
artefacts of every type were rendered from the test data and read.

## Requirements traceability

| Brief item | Where |
|---|---|
| Four report types, reusing existing services | `reports/build.py`; no engine code changed |
| Evidence-backed claims with IDs, hashes, locations, contradictions | claim structure, `test_case_report_…`, `test_evidence_manifest_…` |
| Currencies and accounts separate; no double counting; totals reconcile | `test_reconciliation_report_…`, `test_currencies_and_grouped_matches_…`, `test_linked_values_…` |
| Exposure / unmatched / score labelled; scores not probabilities | terminology, notes on every total, P9 claim text |
| Stale runs not current; unconfirmed OCR not facts; unavailable balances stated | `test_stale_run_…`, `test_unconfirmed_ocr_…`, balance-check limitation |
| Memo sections, draft only, recommendations labelled, no fabricated approval | `test_memo_is_a_draft_…`, vocabulary `approval_supported: false` |
| JSON + human-readable; escaping; no path traversal; safe names; bounded size | JSON / HTML / MD, `test_html_…`, API tests, `test_oversized_…` |
| Provenance (time, schema / config, selection, record IDs, hashes) | envelope + `provenance`, register row |
| Versioned, never overwritten; sources unmodified | `test_deterministic_…`, `test_reports_never_write_…` |
| Protected API, validation, pagination; UI driven by the API | API tests, A14, `reports-ui.js` |
| No ground truth or labels at runtime; P9 unchanged | `test_reports_never_write_…` |

**Formula injection:** not applicable. P16 provides no CSV export; the note with a formula payload in the tests
is reproduced as text in JSON, HTML and Markdown.

## Deviations from the roadmap entry

- **No PDF (and no DOCX).** The roadmap names ReportLab, which is not installed, and the brief allows PDF only if
  existing dependencies support it safely. No dependency was added. The HTML rendering is printable to PDF from
  a browser, but no PDF artefact is produced or hashed. "The PDF opens and its hash is stored" is therefore not
  met; the HTML and Markdown artefacts' hashes are stored instead.
- **Templates in code, not configuration.** A configuration section would change the configuration hash that
  every P9 result is keyed on.
- **Memo source.** The report model is built from P15 investigations (the workflow record) rather than a
  separate findings snapshot. Dismissed findings are excluded from the material findings and listed separately
  ("excluded from reports but retained").

## Limitations

- **Approval:** no workflow exists. Memos stay drafts, and nothing approves or signs them.
- **Formats:** no PDF or DOCX output.
- **Narrative:** template-based. Analyst text is reproduced verbatim and not verified; a claim being
  `traceable` does not make it true.
- **Values:** linked-transaction values cover direct transaction and P9 risk-row links only. Amounts inside
  linked reconciliation results are reported in their reconciliation context.
- **Truncation:** listings are bounded at 500 rows per section. Totals always cover every row, and truncation
  is stated.
- **Stale runs:** only the summary recorded at the time is shown; row-level results are not reconstructed.
- **Run comparison:** none.
- **OCR test:** the OCR-specific test needs a local Tesseract and is skipped where it is missing (it ran here).
