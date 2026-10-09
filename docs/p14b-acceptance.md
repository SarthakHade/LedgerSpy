# P14b — PDF Bank Statements and OCR: Acceptance Record

Date: 2026-10-10 · Scope: [`roadmap.md`](roadmap.md) §P14 (P14b), on top of P14 (`65d302b`) ·
Methodology: [`pdf-extraction.md`](pdf-extraction.md).

**What P14b adds.**
- **Ingestion.** PDF bank statements through the existing P3 upload and evidence flow (`kind=bank_statement`).
- **Extraction.** Local text extraction (PDFium) and local OCR (Tesseract) per page, including mixed PDFs. It
  runs in a time-limited subprocess with page, pixel and time limits.
- **Layout.** One explicit, testable strategy (`header_columns`) that keeps every line with a class and its
  provenance.
- **Trust.** Separate text / OCR / parse confidences, review-required rules, and statement checks that run only
  when the semantics are known.
- **Review gate.** Immutable extractions and append-only reviewer decisions. The P3 confirmation is refused
  until the extraction is complete and every uncertain row is decided.
- **P14 integration.** Unchanged matching, with a guard against unconfirmed lines and PDF provenance in results.
- **Interfaces.** Seven protected endpoints (plus the P3 upload / mapping) and an extraction-review panel in the
  Bank Recon tab that draws the original page.

**What it leaves alone.**
- P9 is unchanged (same `result_sha256`), and nothing reads labels, ground truth or model output.
- P15 is not started: there is no value-correction or case workflow.
- P14 matching thresholds are unchanged.

## Dependencies (user-approved, installed for this phase)

| Component | Version | How | Offline |
|---|---|---|---|
| `pypdfium2` (PDFium 6462) | 4.30.0 | pip, pinned in `pyproject.toml` | Self-contained wheel, no dependencies |
| `Pillow` | 11.3.0 | pip, pinned | Self-contained wheel, no dependencies |
| Tesseract + `eng` | 5.4.0.20240606 (Leptonica 1.84.1) | `winget install UB-Mannheim.TesseractOCR` (system executable, not on PATH; found via `pdf.ocr.executable_candidates`) | Installer / OS package; see [`pdf-extraction.md`](pdf-extraction.md) §1 |

**Library choice.** The roadmap named pdfplumber. pypdfium2 + Pillow were chosen instead, as two self-contained
wheels with no transitive dependencies; PDFium also gives the character boxes and page rendering the design
needs. No cloud or remote service is used.

## Commands and results (final state)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .   # All checks passed
.venv/Scripts/python -m pytest                                       # 1122 passed, 0 failed, 0 skipped (1088 P0–P14 + 34 P14b), 19 min 51 s
node tests/frontend/harness.js                                       # 13 passed, 0 failed
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py         # 18 passed, 0 failed (16 earlier + 2 P14b)
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py # 6 passed, 0 failed
```

**Genuine OCR was executed.**
- `test_genuine_local_ocr_on_a_scanned_page`, `test_mixed_digital_and_scanned_pages`,
  `test_missing_language_data_is_reported` and the OCR dataset tests run the real local Tesseract 5.4.0. They
  are marked to skip with a stated reason only where Tesseract is absent.
- On this machine none was skipped (0 skipped in the suite).
- Unavailable OCR is tested by pointing the configuration at a non-existent executable, not by mocking.

| P14b test file | Tests | What it proves |
|---|---|---|
| `test_pdf_units.py` | 23 | **Digital extraction:** fields, continuation merging, account / currency context, page and box provenance, coverage, stated opening / closing checks. **Behaviour:** deterministic re-processing; balance discontinuity and stated-closing mismatch flagged; Debit / Credit and Amount + Dr/Cr layouts leave semantics unconfirmed; invalid dates kept as `unparsed` and invalid amounts flagged; debit + credit both set and no amount; several date formats; multi-page headers, footers and carried columns; ambiguous and unrecognised layouts reported; header vocabulary; a text layer over an image never auto-accepted. **Review rules:** OCR always review-required, low confidence listed, glyph / straddle / carried reasons. **Separator** decision; **checks** only with known semantics. **OCR:** unavailable → `not_processed`, nothing invented; missing language data; **genuine Tesseract OCR** of a scanned page; mixed digital + scanned pages. **Safety:** malformed / truncated / not-PDF / encrypted / too many pages; render limit refuses instead of downscaling; an oversized scanned page is not processed; no network, P9, label or ground-truth imports |
| `test_pdf_datasets.py` | 11 | **Evidence and extraction:** PDF bytes and SHA-256 preserved; extraction immutable. **Accuracy:** a 784-row, 20-page statement extracted with **0 field differences** from its source and all checks matching. **Reconciliation:** the confirmed PDF reconciles exactly like the same statement from CSV (identical status counts and matched amounts); results carry PDF provenance; P9 `result_sha256` unchanged. **Guard:** an unconfirmed extracted line never enters matching. **OCR review gate:** confirmation refused while pending; unparsed lines can only be excluded; exclusions respected; decisions append-only; review closed after confirmation. **Failure handling:** OCR unavailable → `incomplete`, confirmation refused, re-extraction completes, identical re-extraction registers nothing; a killed (timed-out) extraction registers nothing and the evidence stays intact. **Hostile input:** malformed, encrypted, too many pages and oversized files rejected with nothing registered. **Isolation:** two statements (accounts) in one PDF kept apart, and a currency never assumed. **API:** token on every endpoint, upload, pagination, limits, item detail, page PNG, DPI and page bounds, hostile IDs, review validation, OCR status, ledger PDFs still refused |
| `browser_check.py` (+2, A12) | 2 | The review panel shows exactly the API's review-required rows, per-page methods and the OCR engine; the original page is drawn on the canvas from the API; a reviewer decision is stored through the API |

**Earlier tests changed (necessary, P14b changes the behaviour they pinned).**
- `test_recon_units.py::test_pdf_statements_are_deferred_to_p14b` becomes
  `test_pdf_is_accepted_only_as_a_bank_statement_and_malformed_pdfs_are_refused`. P14 refused all PDFs; ledgers
  still are, and bank-statement PDFs are now extracted, so a malformed one gets `pdf_malformed`.
- `test_recon_units.py::test_migration_6_keeps_existing_datasets_and_triggers`: the pinned
  `SCHEMA_VERSION == 6` becomes "latest (≥ 6)". The same v5 database now migrates through 6 and 7, and every
  row, trigger and FK assertion is kept.
- `test_frontend_static.py::test_unavailable_modules_are_labelled_not_simulated`: "≥ 4 not-available cards"
  becomes "≥ 3". Bank reconciliation including PDF / OCR is no longer unavailable. An assertion was added that
  the card states the OCR engine status from `/api/pdf/ocr-status`.
- `browser_check.py`:
  - A5's `recon` flag checks that the OCR-status text comes from the API, instead of a "not available" card.
  - A11's label was renamed.
  - A12 is new.
- `conftest.py` is unchanged.

**Earlier code changed.**
- **DB:** migration 7 rebuilds `datasets` with `format IN ('csv','xlsx','pdf')` (same columns, rows and
  triggers). It adds `pdf_extractions` and `pdf_extraction_items` (immutable) and `pdf_review_decisions`
  (append-only).
- **Ingestion:**
  - `detect_format(allow_pdf=…)`: PDFs only for bank statements.
  - The reader serves the in-memory extracted table (`format 'pdf'`).
  - `ingest_upload` / `confirm_mapping` branch to the PDF service and gate.
- **P14:**
  - `records.bank_rows`: the `unconfirmed_extraction` guard and an `extraction` summary on PDF lines.
  - `service.result_detail`: `pdf_provenance`.
  - Results get an OCR limitation note.
  - Nothing changes for CSV / XLSX statements.
- **Config:** a `pdf:` section.
- **API:** the `pdf` router; `api.js` gets `blob`.
- **UI:**
  - The review panel.
  - Selects for an ambiguous date format and decimal separator in the statement mapping.
  - A render-generation guard, fixing a race the A12 check exposed: a slow setup request could overwrite the
    review panel.
  - The P14b card now shows OCR status.

## Acceptance criteria

| # | Requirement | Evidence | Result |
|---|---|---|---|
| 1 | Reuse P3 evidence / ingestion; preserve PDF bytes + SHA-256; never overwrite | Upload through the P3 endpoint; stored bytes identical; evidence immutable | ✅ |
| 2 | Validate type, size, pages, malformed, encrypted; explicit errors | `pdf_malformed`, `pdf_encrypted`, `pdf_too_many_pages` (413), `file_too_large` (413); nothing registered | ✅ |
| 3 | Page numbers and coordinates | Every item: page, line, box (PDF points); every cell: box; OCR boxes converted from pixels | ✅ |
| 4 | Detect text / scanned / mixed per page | `text`, `text_over_image`, `ocr`, `not_processed`, `empty` from the page itself | ✅ |
| 5 | Local text extraction; OCR only when needed; engine provenance | PDFium for text; Tesseract only on image pages; versions, languages, DPI, PSM in the extraction | ✅ |
| 6 | OCR unavailable → actionable status, no fake output | Exact reason (no executable / no language data); page `not_processed`; extraction `incomplete`; confirmation refused | ✅ |
| 7 | Bounded work | Page, pixel, per-page OCR and whole-worker time limits; subprocess isolation | ✅ (memory bounded indirectly, not measured) |
| 8 | Fields only when supported; no assumptions | Account / currency / period / opening / closing only from labels; Withdrawal / Deposit direction only from headers; ambiguous date format / separator / Debit-Credit meaning must be confirmed | ✅ |
| 9 | Explicit layout strategy, original text kept | `header_columns`; every line classified with source text; unrecognised layouts reported | ✅ (one strategy; see limitations) |
| 10 | Separate confidences; OCR ≠ correctness | Text / OCR / parse reported separately; every OCR row review-required | ✅ |
| 11 | Validation only with known semantics | Continuity (per account), stated opening / closing, `not_evaluated` with reasons | ✅ |
| 12 | Unparsed lines, headers, footers, carry-forward kept | Classes and coverage per kind; unparsed table lines must be acknowledged | ✅ |
| 13 | Review before trusted input; no new case workflow | Confirm / exclude only; P3 confirmation gated; no value editing (P15) | ✅ |
| 14 | Low confidence cannot produce a misleading match | Gate + `unconfirmed_extraction` guard; OCR limitation on results; thresholds unchanged | ✅ |
| 15 | P14 behaviour preserved; chain to PDF / page / text | PDF and CSV of the same statement reconcile identically; `pdf_provenance` in result detail | ✅ |
| 16 | Protected, paginated API; UI from API data | 7 endpoints, 401 tests, size ≤ 500; A12 | ✅ |
| 17 | Deterministic re-processing | Same configuration → same extraction ID and items hash; nothing new registered | ✅ |
| 18 | Failure leaves evidence intact, no false success | A killed worker registers nothing; evidence hash verified | ✅ |
| 19 | Offline | No network imports (static test); pytest-socket guard; local executables only | ✅ |
| 20 | P9 unchanged | `result_sha256` equal before / after PDF import and reconciliation | ✅ |
| 21 | Genuine OCR integration test | Executed with Tesseract 5.4.0 (not mocked) | ✅ |
| 22 | Full suite / frontend / Ruff | 1122 passed, 0 failed, 0 skipped (1088 P0–P14 + 34 P14b), 19 min 51 s; harness 13/13; browser 18/18; panel 6/6; Ruff clean | ✅ |

## Measured results (this machine, final code)

**Accuracy** (P5-derived statements, generated for tests; scored against the source CSV):

| Document | Pages | Rows | Rows with any wrong field | Review-required | Statement checks |
|---|---|---|---|---|---|
| Digital, 6 pt, 784 rows | 20 (text) | 784 | **0** | 0 (all auto-accepted) | Opening, closing, continuity match |
| Digital, 150 rows | 4 (text) | 150 | **0** | 0 | Match |
| Scanned at 300 dpi (rendered), 6 pt, 200 rows | 5 (OCR) | 200 | **22** (17 balances, 3 references, 2 credits) | **200** (all) | Evaluated; incomplete rows reported |

**How the 22 OCR misreads were caught.**
- **Specific flag (21):**
  - Most were Tesseract inserting a space inside a number (`29,431 ,202.89`), which gives an invalid amount.
  - The others were caught by low OCR confidence.
- **Blanket rule only (1):** a reference read as `SYNR23041 1000050` (for `SYNR230411000050`) is a valid-looking
  value. It is caught only by the blanket "every OCR row needs review" rule. That case is why OCR rows are never
  auto-accepted.

**Runtime** (wall time including the worker subprocess start; second of two runs):

| Document | Size | Time |
|---|---|---|
| Digital, 20 pages / 784 rows | 241 KB | 1.54 s |
| Digital, 163 pages / 6,483 rows | 2.0 MB | 10.1 s |
| Scanned, 3 pages / 120 rows (OCR at 300 dpi) | 1.1 MB | 7.98 s (2.54 s per OCR page) |
| Mixed, 20 pages with 5 scanned / 784 rows | 1.9 MB | 12.7 s (2.29 s per OCR page) |

**Not measured:**
- **Memory:** not instrumented. It is bounded by one page rendered at a time (≤ 40 Mpx) and the worker timeout.
- **Real bank statements:** none were used (no confidential documents are committed). All measurements use
  generated statements, so layouts from real banks are untested.

## Not implemented / deferred (stated, not claimed)

- **Value correction.** A reviewer can confirm or exclude a row, but cannot correct an OCR misread (e.g.
  `UTROOO1` for `UTR0001`). Correction workflows belong to P15.
- **More layout strategies.** Only header-anchored column tables are supported. Not handled:
  - statements without a header line;
  - unknown header wording;
  - multi-column pages;
  - rotated pages;
  - statements where a transaction's amounts sit on a different line than its date.

  These yield `layout_not_recognised` / `unparsed`, never guessed rows.
- **Dr/Cr-signed balances.** These are kept as text, and balance checks are not evaluated for them.
- **Languages.** OCR languages beyond `eng` and handwritten statements are untested.
- **Encrypted PDFs** are refused; no password entry is offered.
- **No hard memory cap.** On Windows there is no per-process memory limit for the worker.
- **No risk-engine integration.** Reconciliation and extraction results are not fed into P9, as required.
