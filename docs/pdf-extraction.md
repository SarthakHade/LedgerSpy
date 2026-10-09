# PDF Bank Statement Extraction and OCR (P14b)

PDF bank statements are read locally, reviewed, and only then confirmed as a P14 bank-statement dataset.

| | |
|---|---|
| Code | `ledgerspy/pdfstatements/` (pages, ocr, layout, validate, extract, worker, service) |
| API | `ledgerspy/api/routes/pdf.py` |
| UI | Bank Recon tab (`frontend/js/recon-ui.js`) |
| Configuration | `pdf:` in `config/default.yaml` |
| Acceptance record | [`p14b-acceptance.md`](p14b-acceptance.md) |

**Ground rules.**
- Everything runs on this machine. There is no cloud OCR, remote API or telemetry.
- The original PDF is evidence: its bytes and SHA-256 are kept unchanged.
- Extracted values are stored separately from the evidence, and every row keeps its page, position and source
  text.
- No value is invented. That covers dates, amounts, currencies, balances, references and account numbers.
- Uncertain rows are review-required and never become trusted input without a reviewer decision.
- OCR confidence is reported but never treated as financial correctness.
- P9 risk is not touched.

## 1. Dependencies and offline installation

| Component | Version tested | Kind | Role |
|---|---|---|---|
| `pypdfium2` | 4.30.0 (PDFium 6462) | Python wheel with the PDFium binary; no dependencies | Validation, embedded text with character boxes, page rendering |
| `Pillow` | 11.3.0 | Python wheel; no dependencies | Page images for OCR and reviewer previews |
| Tesseract | 5.4.0.20240606 (Leptonica 1.84.1), `eng` data | **Local executable**, not a Python package | OCR of scanned pages |

Both wheels are pinned in `pyproject.toml`. For an offline machine:

1. **Download the wheels** on a connected machine:

   ```bash
   pip download pypdfium2==4.30.0 Pillow==11.3.0 -d wheelhouse
   ```

2. **Install them** on the offline machine:

   ```bash
   pip install --no-index --find-links wheelhouse pypdfium2 Pillow
   ```

3. **Install Tesseract and its language data:**
   - **Windows:** the UB Mannheim installer (`tesseract-ocr-w64-setup-5.4.0.20240606.exe`, or
     `winget install UB-Mannheim.TesseractOCR`). It installs to `C:\Program Files\Tesseract-OCR` and includes
     `eng`.
   - **Debian / Ubuntu:** `apt install tesseract-ocr tesseract-ocr-eng`.
   - **macOS:** `brew install tesseract`.
   - **Other languages:** copy their `*.traineddata` into Tesseract's `tessdata` folder and list them in
     `pdf.ocr.languages`.

**Finding the executable.** LedgerSpy tries each `pdf.ocr.executable_candidates` entry in order: a name on PATH,
then the usual install paths. It runs `--version` and `--list-langs`.
- Without the executable or its language data, OCR is **unavailable**, and the API and UI say exactly why.
- Text PDFs still work. Scanned pages are reported as `not_processed`, the extraction is `incomplete`, and the
  statement cannot be confirmed until OCR works and the extraction is re-run.

## 2. Pipeline

```
upload (P3: size limit, SHA-256, evidence)  →  worker subprocess (timeout)  →  per page: text | OCR | not processed
  →  words with boxes  →  lines  →  table header  →  columns  →  classified items  →  review flags  →  checks
  →  stored extraction (immutable)  →  reviewer decisions (append-only)  →  P3 mapping confirmation  →  canonical rows
```

**Upload.** It uses the existing endpoint: `POST /api/cases/{case}/files?filename=…pdf&kind=bank_statement`.
- **Accepted:** a `.pdf` is accepted only as a bank statement. Ledgers stay CSV / XLSX.
- **Refused:**
  - A file without a `%PDF-` header (`pdf_malformed`).
  - An unreadable file (`pdf_malformed`, "Data format error").
  - A password-protected file (`pdf_encrypted`). Statements are never decrypted.
  - More pages than `pdf.max_pages` (200; `pdf_too_many_pages`, 413).
  - Larger than `ingest.max_file_bytes` (`file_too_large`, 413).
  - A worker that crashes or runs longer than `pdf.worker_timeout_seconds` (900 s;
    `pdf_extraction_failed` / `pdf_extraction_timeout`).
- **On refusal:** nothing is registered. The staged file is deleted and the rejection is audit-logged, as for
  CSV / XLSX.

**Isolation.** PDF parsing (PDFium, native code) and OCR run in a child process
(`python -m ledgerspy.pdfstatements.worker`), never in the server. The parent kills it at the timeout. A hostile
PDF can at worst crash or stall the child.

**Method per page.** It is decided from the page itself, never assumed:

| Method | When | Trust |
|---|---|---|
| `text` | ≥ `pdf.min_text_chars` (20) non-space characters of embedded text (or any text and no page-sized image) | Exact glyph codes; replacement characters (U+FFFD) are counted and flagged |
| `text_over_image` | Embedded text **and** an image covering ≥ `pdf.image_page_min_coverage` (50 %) of the page | A text layer over a scan, e.g. someone else's OCR: every row review-required |
| `ocr` | Little or no text and a page-sized image | Rendered at `pdf.ocr.dpi` (300), recognised by Tesseract (`--psm 6`, `OMP_THREAD_LIMIT=1`); every row review-required |
| `not_processed` | OCR needed but unavailable, the page would exceed `pdf.render.max_pixels` (40 Mpx; never silently downscaled), or OCR failed / timed out (`pdf.ocr.page_timeout_seconds`, 120 s) | Extraction `incomplete`; cannot be confirmed |
| `empty` | Neither text nor an image | — |

A PDF can mix methods page by page: some pages digital, some scanned.

## 3. Layout strategy `header_columns` (`layout.py`)

There is one explicit, testable strategy; no universal regex is used.

1. **Words and lines.**
   - Embedded text is grouped into words from PDFium character boxes.
   - OCR words come from Tesseract TSV, with pixel boxes converted to PDF points.
   - Boxes are in PDF points with a top-left origin.
   - Lines are formed by vertical centre (`pdf.layout.line_tolerance` × median word height).
2. **Table header.** A line whose cells name known columns, matched against a documented vocabulary:
   - **Roles:** date, value date, narration / description / particulars, reference / cheque / UTR, withdrawal /
     debit, deposit / credit, amount, Dr/Cr, balance.
   - **Requirement:** a date, an amount source (debit + credit, or amount), and ≥ `min_header_roles` (3) roles.
   - **Rejection:** a role named twice makes the header ambiguous; it is rejected and reported.
   - **Currency:** a code in a header such as "Deposit (INR)" is read as the statement currency.
3. **Column boundaries.** These are measured on the page's own body text.
   - **Search region:** between the end of one header cell and the start of the next, so each header stays in
     its own column.
   - **Boundary:** the middle of the widest vertical whitespace channel in that region (the x positions covered
     by the fewest body words).
   - **Why:** this handles left-aligned narrations that run past their header and right-aligned amounts that
     start left of theirs.
   - **Assignment:** each word goes to the column holding its centre.
   - **Uncertain words:** a word reaching more than 30 % of its width across a boundary is
     `column_assignment_uncertain`.
4. **Line classes.** Every line is kept with exactly one class:
   - `table_header`, `transaction` (a date in the date column), `continuation` (a narration-only line directly
     below a transaction, merged into its narration and also kept separately);
   - `opening_balance`, `closing_balance`, `carry_forward` (b/f, c/f), `total`;
   - `statement_info` (account number, currency, period, stated opening / closing balance, read by label),
     `page_header`, `page_footer` ("Page n of m");
   - `unparsed`, with a reason, such as a date-column value that is not a date in any configured format.
5. **Pages without their own header.** They reuse the previous page's columns only for lines with a date where
   the date column is. Those rows are `columns_carried_from_previous_page` (review-required).
6. **No recognised header on a page.** The page yields no transactions and is reported as
   `layout_not_recognised`. Nothing is guessed from free text.

**Statement context.** Account number and currency apply to the transactions that follow them, until changed.
One PDF may hold several statements (accounts), and each row keeps its own.

## 4. Fields, confidence and review (`validate.py`)

Three kinds of confidence are reported separately, and none is a measure of financial correctness:

| Confidence | Meaning |
|---|---|
| Text | `text`, `text_over_image` or `ocr`, plus replacement-character counts |
| OCR | Tesseract word confidence 0–100 (glyph recognition), the minimum per row, and words below `pdf.ocr.min_word_confidence` (70) |
| Parse | Per field: `ok`, `missing`, `invalid` or `unknown_separator`. Dates use the configured `mapping.date_formats`; amounts use the statement's decimal separator |

**Decimal separator.** It is decided per statement from the evidence of the amount cells.
- **Votes:** values that parse under exactly one reading vote for it. One reading must hold ≥
  `mapping.min_parse_rate` (98 %) of the votes. Values that parse under both readings must agree.
- **Source:** embedded-text rows are used when there are any, so OCR noise cannot make the convention
  ambiguous. Unparseable cells are flagged on their own rows.

**Review-required reasons** (a row with any reason must be decided):
- `ocr_derived`: every OCR row, whatever its confidence.
- `low_ocr_confidence`, `text_layer_over_image`, `unmapped_glyphs`, `column_assignment_uncertain`,
  `columns_carried_from_previous_page`.
- `invalid_value_date`, `invalid_debit` / `credit` / `amount` / `balance`, `decimal_separator_ambiguous`.
- `debit_and_credit_both_set`, `no_amount`, `balance_discontinuity`.
- `unparsed_line_in_table`: such a line may be a missed transaction and must be acknowledged as excluded.

Digital rows with no reason are `auto_accepted` (`pdf.auto_accept_text_rows: true`). With `false`, every row
needs review.

**Statement checks.** These run only when the semantics are known:
- **Prerequisites:**
  - The headers say Withdrawal / Deposit (or, after confirmation, the confirmed `bank.debit_means`).
  - The decimal separator is known.
  - Balances carry no Dr/Cr indicators.
  - There is no single signed amount column.
- **Not evaluated:** otherwise the result is `not_evaluated`, with the reason.
- **Checks:**
  - Balance continuity, per account.
  - Implied opening balance vs the stated one.
  - Last balance vs the stated closing balance.
  - Opening + movement = closing, when every row is complete.
- **Breaks:** each break flags its row. With several accounts in one PDF, stated balances are not attributed and
  not compared.

## 5. Data states, review and confirmation (`service.py`)

| State | Where | Mutability |
|---|---|---|
| **Evidence** | `evidence_files` + read-only file (P3) | Never changed |
| **Extracted** | `pdf_extractions` (method per page, engine versions, configuration hash, coverage, checks) + `pdf_extraction_items` (every line: page, box, source text, cells, fields, confidences, reasons) | Immutable; re-extraction adds a new extraction |
| **Reviewed** | `pdf_review_decisions`: `confirm` or `exclude` per review-required item, with reason, reviewer and time | Append-only; the latest decision counts until confirmation |
| **Confirmed / normalised** | Canonical transactions via the P3 mapping confirmation, then P4 normalisation | P3 / P4 rules |

**IDs.**
- **Extraction ID:** `PE-` plus a hash of dataset, evidence SHA-256, extraction configuration and engine
  provenance (PDFium / Tesseract versions, languages, DPI, PSM). The same inputs give the same ID; re-extraction
  with identical configuration registers nothing new.
- **Item ID:** `PX-` plus a hash of evidence SHA-256, page and line number. It becomes the canonical
  transaction ID.

**Reviewer decisions.**
- A review-required transaction can be **confirmed**: "the extracted values match the page".
- It can be **excluded**: kept with its reason, but not in the dataset.
- Unparsed lines can only be excluded ("acknowledged, not a transaction").
- Values are **not edited** in P14b. Correction workflows belong to P15; after confirmation, P4 logged
  transformations are the existing correction mechanism.

**Confirmation.** `PUT /api/datasets/{id}/mapping` (the P3 confirmation) is **refused**:
- while the extraction is incomplete (`pdf_extraction_incomplete`, 409);
- while any review-required item has no decision (`pdf_review_pending`, 409);
- when no row would remain (`pdf_no_trusted_rows`).

**The extracted table.** It is handed to the standard P3 proposal and validation:
- **Columns by role:** `Txn Date`, `Value Dt`, `Narration`, `Ref No`, `Withdrawal` / `Deposit` or `Debit` /
  `Credit`, `Amount`, `Dr/Cr`, `Balance`.
- **Direction:** `Withdrawal` / `Deposit` only when the PDF's header says so, which lets the bank proposal state
  the direction. Otherwise `bank.debit_means` must be confirmed.
- **Optional columns:** `Currency` and `Account No` appear only if the statement states them. A missing currency
  must be declared or the rows stay insufficient data in P14.
- **Formats:** an ambiguous date format (02/01/2024) or decimal separator must be confirmed.
- **Provenance in `extra`:** `pdf_page`, `pdf_line_bbox`, `pdf_text`, `pdf_method`, `pdf_min_ocr_confidence`,
  `pdf_review` (`auto_accepted` / `reviewer_confirmed`) and `pdf_extraction_id`.

**P14 integration.**
- The PDF dataset is an ordinary bank statement for P14. Matching thresholds are unchanged.
- **Defensive guard:** a bank line whose `pdf_review` is not trusted is `insufficient_data`
  (`unconfirmed_extraction`) and never matched.
- **Results:** each result lists the PDF provenance of its bank lines. OCR-derived lines carry a limitation
  saying they were read by OCR and confirmed by a reviewer.

## 6. API

All endpoints are token-protected, paginated (`size` ≤ 500) and validated by pattern; none takes a path.

| Method | Path | |
|---|---|---|
| POST | `/api/cases/{case}/files?filename=x.pdf&kind=bank_statement` | Upload + extraction (P3 endpoint) |
| GET | `/api/pdf/ocr-status` | Tesseract availability, version, languages |
| GET | `/api/datasets/{id}/pdf` | Extraction summary: status, engines, pages, coverage, checks, review counts, data states, history |
| GET | `/api/datasets/{id}/pdf/items` | `kind`, `review` (required / pending / confirmed / excluded / auto_accepted / not_reviewable), `pdf_page`, pages |
| GET | `/api/datasets/{id}/pdf/items/{PX-…}` | Item with decisions |
| POST | `/api/datasets/{id}/pdf/review` | `{decisions: [{item_id, decision, reason}]}` (≤ 500; all-or-nothing) |
| POST | `/api/datasets/{id}/pdf/extract` | Re-run with the current configuration (before confirmation) |
| GET | `/api/datasets/{id}/pdf/pages/{n}/image?dpi=` | PNG of the original page (≤ `render.max_preview_dpi`, 200) |
| PUT | `/api/datasets/{id}/mapping` | P3 confirmation (gated as above) |

**Page images.** They are fetched with the bearer token and drawn on a `<canvas>` (`createImageBitmap`), so the
CSP (`img-src 'self'`) is unchanged and no token travels in a URL.

## 7. Limitations and known failure modes

- **Layouts:**
  - Only header-anchored column tables are supported.
  - Not recognised:
    - statements without a header line;
    - headers with unknown wording;
    - narrations or amounts spanning columns in unusual ways;
    - rotated pages;
    - multi-column page layouts.
  - Such pages are reported as `layout_not_recognised` / `unparsed` (nothing guessed). Unknown header wording
    could be added to the vocabulary in `layout.py`.
- **OCR misreads:** OCR can misread characters (`0` vs `O`, a space inside a number such as `29,431 ,202.89`).
  - **Flagged:** such cells are invalid, and the row is review-required with its page image.
  - **Not flagged by parsing:** a misread that still forms a valid value, e.g. reference `UTR0001` read as
    `UTROOO1`. Only the reviewer, comparing with the page, can catch it.
  - **Corrections:** values cannot be corrected in P14b. Excluding and re-entering the row is not possible
    either; correction belongs to P15.
- **Language:** OCR language is English (`eng`) unless more language data is installed. Non-Latin scripts and
  handwriting are untested.
- **Dr/Cr balances:** balances with Dr/Cr suffixes are kept as text (`Balance (Dr/Cr)`), and the balance checks
  are not evaluated for them.
- **Encryption:** encrypted PDFs are refused, never cracked. Owner-password-only PDFs that open without a
  password are read.
- **Memory:** there is no hard memory limit. Memory is bounded indirectly by `max_pages`, `max_pixels`
  (one page rendered at a time) and the worker timeout.
- **Determinism:** it is verified for the same machine, configuration and engine versions. A different
  Tesseract version gives a different extraction ID, and may give different text.
