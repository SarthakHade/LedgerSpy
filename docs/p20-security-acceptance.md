# P20 — Security Hardening: Threat Model and Acceptance Record

Date: 2026-10-11 · Scope: [`roadmap.md`](roadmap.md) §P20, on top of P16 (`7c97921`). P17–P19 (optional /
data-gated) are not built; P21 is not started.

**What P20 is.** A focused hardening pass:
- an audit of the existing controls (P2 security, P3 ingestion, P11 models, P14b PDF / OCR, P15 audit, P16
  reports);
- fixes for the seven issues found (§3);
- regression tests (`tests/backend/test_security_p20.py`);
- the roadmap's strict CSP.

It is **not** a claim that LedgerSpy is "secure" in general. §5 lists what is not protected.

## 1. Threat model

**Deployment assumed.** One analyst on one workstation:
- `ledgerspy serve` runs as that user, bound to `127.0.0.1`;
- the analyst's browser opens the printed link;
- the workspace (database, evidence, reports) lives in that user's profile;
- no network service is used.

**Assets.**

| Asset | Property to protect |
|---|---|
| Evidence files (`workspace/evidence/`) | Bytes unchanged; SHA-256 verifiable |
| Canonical rows, P4 transformations, findings inputs | Immutable / logged; not altered by the UI or API |
| Investigation records, decisions, notes, reports | Append-only; attributable; reproducible |
| Audit log | Tamper-**evident** (not tamper-proof) |
| Session token | Never exposed to other origins, logs or the database |
| Analyst's browser | No script execution from imported data |

**Adversaries and entry points.**
- **A1 — hostile content:** a crafted CSV / XLSX / PDF, OCR text, transaction descriptions, file names, notes
  or report text, reaching the parsers, the database, the UI and generated reports.
- **A2 — another website** open in the same browser: CSRF, DNS rebinding, cross-origin reads.
- **A3 — another local process or user without the token,** calling the loopback port.
- **A4 — a local administrator** (or malware running as the user) who can read and write the workspace files
  and the code.

**In scope:** A1, A2, A3. **Out of scope:** A4, as far as *prevention* goes. Against A4 LedgerSpy offers
*detection* only for what §4 lists, and nothing that A4 cannot undo.

**Trust boundaries.**
- browser ↔ local server: token, Host, Origin and Fetch Metadata checks, plus CSP;
- server ↔ uploaded bytes: the parsers, size and structure limits, and the PDF / OCR subprocess;
- server ↔ workspace files: content-addressed names, read-only evidence, generated artefact names only;
- server ↔ SQLite: parameterised SQL, append-only triggers, hash-chained audit log.

## 2. Controls reviewed

| Area | Control (as built) | Verified by |
|---|---|---|
| Authentication | Every `/api` operation except `/api/health` needs `Authorization: Bearer <per-launch token>` (≥ 32 chars, constant-time compare). The token is never accepted from a cookie, the query string or another header. `/api/openapi.json` is protected; `/docs` and `/redoc` are off | `test_every_api_route_requires_the_token` (all 93 operations × 5 bad credentials), `test_token_only_from_the_authorization_header`, P2 tests |
| Page session | `/?token=` is exchanged once for an `HttpOnly; SameSite=Strict` cookie that only unlocks the HTML page (not the API) | P2 tests |
| Network exposure | Bind address must be `127.0.0.1` (config schema + `build_server`); no `--host` flag; `proxy_headers=False`; no access log | config tests; code review |
| Host / Origin / CSRF | Host must be `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding); a present Origin must match; `Sec-Fetch-Site` cross-site is refused | `test_host_origin_and_fetch_metadata_checks`, P2 tests |
| CSP and XSS | `script-src 'self'; style-src 'self'` (§3 V6). Every UI renderer uses `textContent` / DOM APIs; the chatbot escapes every API value. Report HTML carries its own no-script CSP with a hashed stylesheet | `test_served_page_has_a_strict_csp…`, `test_a_page_with_inline_markup_is_refused…`, browser A3 / A13 / A14 (hostile CSV, note, title rendered as text), A4 (no CSP violation), Node harness (no `innerHTML` with data) |
| Request bounds | Bodies ≤ 8 MiB (V2). Pagination: `size` ≤ 500 / 5000, `page` ≤ 10⁸ (V1). Every request model forbids unknown fields; identifiers are validated by pattern | `test_oversized_bodies…`, `test_huge_page_numbers…`, P15 / P16 API tests |
| SQL | All values bound as parameters. The few dynamic fragments are fixed strings or schema names (reviewed `noqa: S608` sites) | review; Ruff `S608` |
| Files and paths | Evidence stored under its SHA-256, read-only, re-hashed before use. Upload names are display metadata only (V3). Report artefacts are named from generated IDs, created exclusively and re-hashed on read. Static files never leave `frontend/`. No endpoint takes a path | `test_static_files_never_leave_the_frontend_folder`, `test_unsafe_original_file_names…`, P3 evidence tests, P16 artefact tests |
| Hostile files | XLSX: macro / OLE / ActiveX / external links / DTD refused; zip-bomb, entry-count and dimension limits; zip-layer errors are explicit (V5). CSV: strict quoting, size / row / column limits. PDF: page and pixel limits, subprocess with timeout, encrypted / malformed refused | P3 / P14b suites, `test_mutated_csv_and_xlsx…`, `test_mutated_pdfs…` |
| Audit | Hash chain; append-only triggers on every record table; **trigger integrity check** and **external anchor** (V4) | §4 tests |
| Models and analytics | Model artefacts are JSON only (no pickle), located by validated ID, with a canonical SHA-256 pinned in the registry, schema-checked, and never loaded from uploads. Labels and ground truth never reach features, detectors, P9 or reports | `test_model_artifact.py` (`test_tampering_is_detected`, `test_loading_never_executes_code`, `test_model_files_are_located_by_validated_id_only`), label-isolation tests in P6 / P7 / P8 / P16 |
| Secrets | The token is printed once to the terminal (the entry link), never logged by the server, never written to the database or audit log | `test_audit_entries_never_contain_the_session_token` |
| Errors | Explicit error codes; no tracebacks; no workspace paths in API responses | `test_error_responses_do_not_reveal_paths…` |
| Dependencies | 9 pinned runtime packages (FastAPI, Starlette, Uvicorn, Pydantic, PyYAML, openpyxl, et_xmlfile, pypdfium2, Pillow); none added in P20. No network client library; tests run with `pytest-socket` | `pyproject.toml`; network-blocking tests |

## 3. Issues found and fixed

| # | Issue | Impact | Fix | Regression test |
|---|---|---|---|---|
| V1 | `page` had no upper bound: `?page=10**20` made SQLite's OFFSET overflow and returned **HTTP 500** on `/api/investigations`, `/api/reports` and other SQL-paged lists | Unhandled error; noisy failure on hostile input | One shared `Page` type (`1 ≤ page ≤ 10⁸`) on all 23 paginated routes → 422 | `test_huge_page_numbers_are_refused_not_server_errors` (every paginated route) |
| V2 | No request-body limit: a 20 MB JSON body was read whole (and echoed in the 422); P15's decision `payload` dict was unbounded | Memory exhaustion by any token holder / buggy client | `LocalSecurityMiddleware` refuses a declared `Content-Length` > 8 MiB (413) before reading. A body without a length is buffered only up to 8 MiB, then refused (413); an invalid length gives 400. The streamed upload keeps its own `ingest.max_file_bytes` | `test_oversized_bodies_are_refused_before_parsing`, `test_upload_keeps_its_own_streaming_limit` |
| V3 | Original file names were stored verbatim, including control characters, line breaks and **bidi overrides**, e.g. `inv` + U+202E + `csv.exe`, which displays as "invexe.vsc" | Spoofed evidence names in the UI, the audit log and reports | `check_original_filename` refuses C0 / C1 controls, U+2028 / U+2029, bidi embedding / override / isolate controls, blank names and > 255 characters, for datasets and master files. Path separators and dots stay allowed: the name is never a path, and P3's `test_filename_is_metadata_never_a_path` keeps `..\..\evil name.csv` as metadata (a first version refused separators, broke that P3 contract and was narrowed). Other Unicode (e.g. Devanagari with ZWJ) is kept | `test_unsafe_original_file_names…` (9 names, nothing stored), `test_path_like_names_stay_metadata`, `test_ordinary_unicode_file_names_are_kept`, `test_the_api_reports_an_unsafe_file_name` |
| V4 | `audit verify` checked only the audit log's two triggers. A dropped or neutered append-only trigger on any other table (P15 notes, P16 reports, evidence, …) went unreported. There was no way to detect truncation | Weakened immutability went unnoticed | `check_triggers` compares every trigger, with its exact SQL, against the set the migrations create (53 at schema 9): missing, altered and unexpected are reported. `/api/audit/verify` and `ledgerspy audit verify` fail on any difference, and return `head_seq` + `head_hash`. The API accepts an **anchor** (`anchor_seq`, `anchor_hash`) recorded earlier outside the database | `test_trigger_set_matches…`, `test_dropped_altered_or_extra_triggers_are_detected` (API, function and CLI), `test_api_verify_fails_when_a_trigger_is_dropped`, `test_truncation_is_invisible_without_an_anchor_and_caught_with_one`, `test_edit_of_an_audit_entry…` |
| V5 | A malformed XLSX could make Python's `zipfile` raise `OSError` (and potentially `zlib.error`, `EOFError`, `NotImplementedError`, …) inside the structural check: an **unhandled exception** (HTTP 500) instead of a refusal. Found by the P20 mutation fuzz | Crash on hostile input | Any zip-layer failure during inspection becomes `xlsx_invalid` (422) | `test_mutated_csv_and_xlsx_never_crash_the_importer` (120 seeded mutations), `test_mutated_pdfs_never_crash_the_importer` |
| V6 | The UI's CSP allowed 22 inline event handlers (by hash, `'unsafe-hashes'`) and **all inline styles** (`style-src 'unsafe-inline'`) | Injected markup could carry styles; handler hashes were a standing exception | Handlers → `data-tab` / `data-action` with one delegated listener. The 145 static `style=""` attributes → `data-style`, applied once through the CSSOM (allowed by CSP, same precedence as before). `el()` sets styles through the CSSOM. CSP is now `script-src 'self'; style-src 'self'`, and the server **refuses to start** if `index.html` contains any inline handler, style attribute, inline script or `<style>` | `test_served_page_has_a_strict_csp…`, `test_a_page_with_inline_markup_is_refused_at_startup`, P2 CSP test (tightened), browser A4 (no CSP violation across the whole run) |
| V7 | Generated report HTML allowed `style-src 'unsafe-inline'` | An inline style smuggled into a report page would apply | The report's CSP allows only its own stylesheet, by SHA-256 | `test_report_html_allows_only_its_own_stylesheet`; P16 golden file (CSP line changed) |

**Manual check of V7 in a browser.**
- The golden report HTML was opened in Chromium. Its hashed stylesheet applies: the banner's border and
  background colours and the body font match the stylesheet.
- A `<style>` element and a `style` attribute injected afterwards had no effect (computed colours unchanged),
  so the report's CSP is enforced.

**Also added.**
- A Trojan-source guard: no bidi control characters anywhere in source, UI, config or docs
  (`test_no_invisible_direction_characters_in_the_source_tree`).
- The guard caught literal bidi characters introduced while writing these fixes and tests; they were replaced
  by escape sequences.

## 4. Audit trail: what is and is not detected

| Change | Detected? | How |
|---|---|---|
| Application write to an append-only table (UPDATE / DELETE) | **Prevented** | SQLite triggers (tests in P2–P16) |
| Edited, inserted, reordered or deleted (non-tail) audit entry | **Yes** | Hash chain (`test_edit_of_an_audit_entry…`) |
| Dropped, altered or extra trigger on any table | **Yes** (P20) | `check_triggers` |
| Truncation of the newest audit entries | **Only with an anchor** | Undetectable from the database alone (tested). Detected when an earlier `head_seq` / `head_hash` recorded outside the database is passed back as an anchor |
| Consistent rewrite of the whole chain (by someone controlling file and code) | **Only with an anchor** | The rewritten chain verifies; an external anchor does not match (tested) |
| A record edited while its trigger was dropped and then **restored exactly** | **No** | Tested and documented (`test_edits_while_a_record_trigger_is_dropped_and_restored_are_not_detected`). Evidence **bytes** are still re-hashed on use, and reports re-verify their references, but metadata edits like this are invisible |
| Edited evidence file | **Yes** | SHA-256 re-check before use, `ledgerspy evidence verify`, report manifests |
| Edited report artefact | **Yes** | SHA-256 recorded in the append-only register, checked on every read |

The audit log is **tamper-evident against edits through the database's normal interfaces and against casual
edits. It is not tamper-proof against an administrator** who controls the database file and the code. An
anchor kept outside the workstation is the only mitigation for truncation and rewrites.

## 5. Not enforced / remaining risks

- **A4 (local administrator or malware as the user)** can read the workspace and read the session token from the
  running process or the terminal. They can also edit the database or replace the code. Full-disk encryption
  and OS account separation are the recommended controls; LedgerSpy implements neither.
- **Memory limits:** the PDF / OCR worker subprocess and Tesseract have **no memory cap** (timeouts, page and
  pixel limits only); XLSX parsing is bounded by the zip / dimension / row limits, not by a memory quota. A
  very large legitimate file can still use a lot of memory.
- **Token in the entry URL:** the printed link carries the token. It is exchanged and redirected immediately, but
  it can remain in the terminal scroll-back and in the browser's history entry for the link.
- **No HTTPS:** the session cookie cannot be `Secure` on plain-HTTP loopback. Traffic never leaves the machine.
- **Any token holder has full access;** there are no roles. Running the app with another binding (e.g. a
  hand-written `uvicorn` command around `create_app`) bypasses the loopback guard of `build_server`. The Host
  check still requires a loopback Host header, but that header can be forged by a remote client.
  **Unsupported: never expose the port.**
- **`pip-audit` was not run:** it needs network access, which this offline project avoids. Dependency auditing is
  left to the connected build machine (P21 packaging).
- **The browser's own extensions** can read the page and its token; out of scope.
- **The parser fuzz is a seeded smoke fuzz** (132 mutations: 60 CSV, 60 XLSX, 12 PDF), not coverage-guided
  fuzzing.

## 6. Behaviour changes (documented security fixes)

- **Pagination:** `page` above 10⁸ → 422 (was 500 or an empty page).
- **Body size:** request bodies above 8 MiB → 413; an invalid `Content-Length` → 400.
- **File names:** an original file name with control, line-break or bidi characters, blank or over 255
  characters → 422 `invalid_filename`.
- **Audit verify:** `ok` is now false when triggers differ from the migrations. New fields: `chain_ok`,
  `head_seq`, `triggers`, `anchor`.
- **CLI:** `ledgerspy audit verify` also fails on trigger differences and prints the head sequence.
- **Report HTML:** the CSP line changed (golden fixture updated).
- **UI markup:** `data-action` / `data-tab` / `data-style` instead of `onclick=` / `style=`, so browser-check
  selectors moved from `[onclick*=…]` to `[data-tab=…]`. Behaviour and assertions are unchanged.
- **P2 CSP test:** it previously expected at least 10 hashed inline handlers; it now requires none.
- **P9:** scores, detectors and results are unchanged. The configuration and feature-registry hashes are
  identical to P16's, and no code under `ledgerspy/risk`, `features`, `detectors`, `anomaly`, `forensics` or
  `supervised` changed.

## 7. Safe defaults and deployment prerequisites

| Setting | Default | Note |
|---|---|---|
| `server.host` | `127.0.0.1` | Any other value is refused by the configuration schema and by `build_server`; there is no `--host` flag |
| `server.port` | `8765` | Host and Origin checks are tied to it |
| `workspace.path` | `workspace` (relative to the working directory; git-ignored) | Recommended: a folder in the analyst's user profile, on a disk with OS full-disk encryption (BitLocker / FileVault / LUKS), not a shared or synced folder |
| `ingest.max_file_bytes` | 100 MiB | Enforced while the upload streams in |
| `ingest.max_rows` / `max_columns` | 1,000,000 / 512 | |
| `ingest.xlsx.*` | ≤ 500 MiB uncompressed, ratio ≤ 100:1, ≤ 10,000 entries | Zip-bomb limits |
| `pdf.max_pages`, `pdf.worker_timeout_seconds`, `pdf.render.max_pixels`, `pdf.ocr.page_timeout_seconds` | 200 pages, 900 s, 40 Mpx, 120 s | No memory cap (§5) |
| Request body (code constant) | 8 MiB | `ledgerspy/api/limits.py` |

**Prerequisites.**
- Run `ledgerspy serve` as the analyst's own (non-administrator) OS account.
- Keep the terminal showing the entry link private, and close the server when done (the token dies with it).
- Install Tesseract only from the official packages (P14b §1) if OCR is needed.
- Never forward or expose the port, and never run `create_app` under another server or binding.
- Keep the workspace out of source control (already git-ignored) and out of cloud-synced folders.

## 8. Commands and results (final code)

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .
PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider -rs tests/backend
node tests/frontend/harness.js
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/browser_check.py
PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/frontend/integrity_panel_check.py
```

| Check | Result |
|---|---|
| Backend suite | 1197 passed, 0 failed, 0 skipped, 0 errors (exit 0; 1110 s; P16 baseline 1160 + 37 new) |
| P20 tests (`tests/backend/test_security_p20.py`) | 37 passed (in the run above) |
| Node harness | 13 passed, 0 failed |
| Browser check | 22 passed, 0 failed (A4: 189 requests, none external, no CSP violation) |
| P4 integrity panel check | 6 passed, 0 failed |
| Ruff | check clean; format clean (259 files) |
