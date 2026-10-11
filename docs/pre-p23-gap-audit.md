# Pre-P23 Completeness Audit and Gap Closure

Date: 2026-10-11 · Starting point: `main` at `36c9e72` (P22), clean working tree, no other branch.
Scope: every roadmap requirement P0–P22 ([`roadmap.md`](roadmap.md)) and the PS requirements R1–R20
([`requirements.md`](requirements.md)). P23 (documentation) and P24 (final end-to-end validation) are **not
started**.

## 1. Method

1. **Source of truth.** Read these, then checked every uncertain claim in the code, the API routes, the UI, the
   migrations and the tests:
   - the roadmap and [`acceptance-inventory.json`](acceptance-inventory.json);
   - every acceptance record (`p1`–`p22`, `cleanup-log.md`);
   - the design documents (architecture, installation, verification, reporting, security, reconciliation, PDF /
     OCR, investigations, model cards).

   Earlier summaries were not taken as proof. Examples of what was checked directly:
   - the trigger inventory of all 32 tables;
   - the audit-verify route;
   - the frontend modules and their API calls;
   - the CLI parser and workspace resolution;
   - every runtime `assert`;
   - the chatbot boundary;
   - the Monte Carlo and benchmark cards.
2. **Status vocabulary** (exactly one per requirement):

   | Status | Meaning |
   |---|---|
   | **Verified** | Implemented and verified |
   | **Incompletely verified** | Implemented but incompletely verified |
   | **Partial** | Partially implemented |
   | **Not implemented** | Not implemented |
   | **Deferred** | Deferred by design |
   | **Blocked** | Blocked by unavailable data, dependencies or environment |
3. **Classes** for incomplete items:

   | Class | Meaning |
   |---|---|
   | **1** | Release blocker |
   | **2** | Essential missing functionality |
   | **3** | Important but deferrable |
   | **4** | Environment-only verification |
   | **5** | Optional / data-gated |

   Forensic correctness and evidence integrity were ranked above feature count.

## 2. Result in brief

- **Release blockers found:** none in correctness, data integrity or security of the accepted workflows.
- **Gaps fixed here (§5):**
  1. The R6 "interactive similarity matrix" (a PS MUST, and P12's acceptance) had an API but **no UI**.
  2. Three forensic-total invariants were `assert` statements, which `python -O` removes. The P14 record stated
     this as a limitation.
  3. The documented master-data import in the offline bundle **could not work as written**. It omitted
     `--workspace`, so it targeted `.\workspace` of the current folder instead of the workspace `ledgerspy.cmd`
     serves, and the UI did not show the case ID the command needs.
- **Remaining:** deferrable features (§4), environment-only checks (§6) and optional or data-gated phases (§7).
  None of them makes an accepted output wrong. Each is documented and stated in the UI or the API where it
  applies.
- **Recommendation:** see §9.

## 3. Requirement table (P0–P22)

The evidence names the tests or checks that run in `tools/verify.py`. "Browser A*n*" is a check in
`tests/frontend/browser_check.py`.

### Foundation and data (P0–P5)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| P0 | Repository cleanup | Verified | `cleanup-log.md` |
| P1 | Truthful UI: no fabricated values, no label leakage, XSS-safe, no external requests, honest unavailable states | Verified | Node harness (13), `test_frontend_static.py`, browser A3–A9 |
| P2.1 | Config loader, schema validation and hashing | Verified | `test_config.py` |
| P2.2 | Canonical models and the ID scheme | Verified | `test_ids.py` |
| P2.3 | SQLite with `schema_version`; migrations | Verified | `test_db_audit.py`; migration tests for 7→8 and 8→9 |
| P2.4 | Hash-chained audit log; `audit verify` detects edits | Verified | `test_db_audit.py`, `test_security_p20.py` |
| P2.5 | Loopback-only API; session token; security headers; `pytest-socket` | Verified | `test_api_security.py`, `test_server_binding.py`, `test_network_guard.py` |
| P3.1 | CSV and XLSX ingestion with limits | Verified | `test_ingest_*.py` (6 files) |
| P3.2 | Evidence hash and read-only evidence copy | Verified | `test_ingest_*.py` |
| P3.3 | Mapping confirmed by the user | Verified | `test_ingest_*.py` |
| P3.4 | Label split; PaySim imports 10,127 rows | Verified | `test_ingest_*.py` |
| P3.5 | Header rows above the table (title rows) | Not implemented | `p3-acceptance.md` known limitations; §4 G-01 |
| P4.1 | Integrity checks with categorised issues and a readiness formula | Verified | `test_quality_*.py`, P4 panel (6) |
| P4.2 | Logged, reversible fixes; "Auto-fix" | Verified | `test_quality_*.py`, `test_normalize.py` |
| P4.3 | Excluding rows | Deferred | P4 / P15 design: investigation decisions, never a data deletion |
| P5.1 | Seeded synthetic company; ground truth kept apart; every scenario present | Verified | `test_synth_*.py` (5 files) |
| P5.2 | Rings of 3–6 parties | Partial | k = 3 and 4 only (`p5-acceptance.md`); §4 G-02 |
| P5.3 | UI "synthetic" banner | Not implemented | Files, columns and case names say SYNTHETIC; §4 G-03 |

### Detection and scoring (P6–P11)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| P6.1 | Single feature registry; point-in-time property; determinism | Verified | `test_features_*.py` |
| P6.2 | "No constant features" | Deferred | Reported, not removed (P6 deviation 1) |
| P7.1 | Rules and Benford (MAD, z, χ², subgroups, min-n) | Verified | `test_forensics_*.py` |
| P7.2 | Round-amount vendor found by the Benford subgroup analysis | Deferred | Found by the rule instead; 18 rows is below any Benford minimum (P7 deviation 4) |
| P8.1 | Isolation Forest and robust peer z-scores | Verified | `test_anomaly_*.py` |
| P8.2 | Recorded baseline; 100k rows < 60 s | Verified | `BASELINE_HITS`; timing guard |
| P9.1 | Fusion, caps, tiers, volume guard, exact contributions | Verified | `test_risk_*.py`, browser A5 / A6 |
| P9.2 | UI reads the API; JS scorer deleted | Verified | `test_risk_*.py`, browser A5 / A6 |
| P9.3 | "Scenario recall ≥ the target recorded in the plan" | Blocked | No target was ever recorded. Setting one now, after seeing results, would be a fabricated criterion. §4 G-04 |
| P9.4 | Case calendar and approval limits in the served UI | Partial | Configuration only (`--config`); no UI or API. §4 G-05 |
| P10 | Leakage-safe supervised training and evaluation; model card | Verified | `test_supervised_*.py`, `supervised-model-card.md` |
| P11.1 | Hash-verified registry; applicability gating; tampered artefact refused | Verified | `test_model*.py` |
| P11.2 | Supervised signal fused into P9, with SHAP and UI | Deferred | No production-eligible model exists (P11 deviation 1) |

### Entities, graph and reconciliation (P12–P14b)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| P12.1 | Normalisation, blocking, decisions, clusters; look-alikes never merged | Verified | `test_resolution_*.py` |
| P12.2 | Master import (evidence-backed) | Verified | `test_resolution_*.py`. CLI only; the documented command is fixed here (§5.3) |
| P12.3 | **Similarity-matrix UI** with per-feature evidence and shared payment patterns | **Verified (fixed here)** | Browser A15 (3), `test_pre_p23_gaps.py` |
| P12.4 | Analyst confirm / reject that persists | Verified | Recorded as P15 decisions (`test_investigations.py`) |
| P12.5 | Decisions feed back into merging | Deferred | Recorded assessments only (P15 limitation); §4 G-06 |
| P13.1 | Temporal cycles, metrics, evidence; every element traces to transactions | Verified | `test_graph_*.py`, browser A10 |
| P13.2 | Louvain communities; Cytoscape | Deferred | Components and an SVG view instead (P13 deviation 1) |
| P13.3 | Node size = risk, glow = centrality | Partial | Size = exposure, opacity = PageRank percentile, colour = P9 tier (`graph-ui.js`) |
| P14.1 | Staged deterministic matching; matched / partial / missing; side-by-side view; exact P5 counts | Verified | `test_recon_*.py`, browser A11 |
| P14.2 | Row-coverage invariants that also hold under `python -O` | **Verified (fixed here)** | `test_pre_p23_gaps.py` |
| P14.3 | **Exceptions feed the risk engine** (roadmap acceptance) | Deferred | Separate findings family; P9 unchanged by every brief since P14. §4 G-07 |
| P14.4 | Grouping beyond contiguous runs and one counterparty | Partial | `reconciliation.md` §11; §4 G-08 |
| P14.5 | Aging of unreconciled items | Not implemented | §4 G-09 |
| P14.6 | FX matching / currency conversion | Not implemented | Currencies are never mixed (tested); §4 G-10 |
| P14.7 | References of different kinds compared | Not implemented | §4 G-11 |
| P14.8 | Scope proposals without master data | Partial | The analyst maps scopes; §4 G-12 |
| P14.9 | Recorded runs after input changes | Partial | Register entry and hash kept; rows not reconstructed (`inputs_changed`); §4 G-13 |
| P14b.1 | Text and OCR PDF in a time-limited worker | Verified | `test_pdf_*.py`, browser A12 |
| P14b.2 | Per-line provenance; separate confidences | Verified | `test_pdf_*.py`, browser A12 |
| P14b.3 | Review gating: an unconfirmed line is never matched | Verified | `test_pdf_*.py`, browser A12 |
| P14b.4 | Corrections invalidate dependent runs | Verified | `test_investigations.py` (correction tests) |
| P14b.5 | Layouts other than header-anchored columns | Not implemented | Yields `layout_not_recognised`, never guessed rows; §4 G-14 |
| P14b.6 | Real bank-statement layouts validated | Blocked | No real statements available; §6 E-05 |
| P14b.7 | Non-English OCR; handwriting | Blocked | Untested; §6 E-06 |
| P14b.8 | Encrypted PDFs (password entry) | Not implemented | Refused with a message; §4 G-15 |
| P14b.9 | Dr/Cr-signed balance checks | Not implemented | Kept as text; §4 G-16 |
| P14b.10 | Hard memory cap for the OCR worker | Not implemented | §4 G-17 |

### Workflow, reports, security, packaging and tests (P15–P22)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| P15.1 | Case lifecycle, evidence links, append-only notes and decisions, audit entries | Verified | `test_investigations.py` (17), browser A13 |
| P15.2 | Pagination on 100k rows | Verified | `test_pagination_on_100k_rows_is_correct_and_deterministic` |
| P15.3 | Run comparison | Not implemented | §4 G-18 |
| P15.4 | Multi-user identity, roles, assignment, second-person approval | Not implemented | Single local identity; §4 G-19 |
| P15.5 | Some OCR fields correctable after confirmation | Partial | Balance, value-date and Dr/Cr cannot be corrected; §4 G-20 |
| P16.1 | Report model; claims with origin and traceability; totals rebuilt from rows | Verified | `test_reports.py` (21), browser A14 |
| P16.2 | Stale runs shown as superseded | Verified | `test_reports.py`, browser A14 |
| P16.3 | Dismissed findings excluded but retained | Verified | `test_reports.py`, browser A14 |
| P16.4 | Golden templates | Verified | `test_reports.py`, browser A14 |
| P16.5 | **PDF (and DOCX) report file** (R15, SHOULD) | Not implemented | HTML prints to PDF from the browser; §4 G-21 |
| P16.6 | Memo approval / sign-off | Not implemented | Always a draft; §4 G-22 |
| P16.7 | Report comparison across runs | Not implemented | §4 G-18 |
| P16.8 | Report-specific exclusion beyond dismissal | Not implemented | §4 G-23 |
| P20.1 | Strict CSP; malicious fixtures; threat model; token and Origin review | Verified | `test_security_p20.py`; browser A4 (0 CSP violations) |
| P20.2 | `pip-audit` | Blocked | Needs network; §6 E-03 |
| P20.3 | Formula-injection-safe exports | Not applicable | No CSV / spreadsheet export exists; HTML and Markdown are escaped (`test_html_and_markdown_escape_hostile_text`) |
| P20.4 | Truncation detection | Partial | Needs an anchor recorded outside the database; §4 G-24 |
| P20.5 | Protection against a local administrator or malware | Deferred | Detection only; out of scope by design (threat model A4) |
| P20.6 | Token in terminal and browser history | Deferred | Documented residual risk |
| P21.1 | Hashed wheelhouse; offline install; `doctor`; workspace lock; backups | Verified | `test_packaging_p21.py`, `package`, `offline-bundle` |
| P21.2 | Fresh install on a network-disabled machine; zero non-loopback connections | Incompletely verified | Process-level network guard and netstat (26 / 26); the machine's adapter was not disabled; §6 E-01 |
| P21.3 | PyInstaller executable; interpreter bundled | Not implemented | Folder bundle needing CPython 3.12; §4 G-25 |
| P21.4 | Non-Windows bundles | Blocked | §6 E-04 |
| P21.5 | Tesseract shipped | Deferred | A separate local prerequisite, documented (installation §5) |
| P22.1 | One command; honest exit codes; package provenance and reproducibility | Verified | `test_verify_p22.py` (21) |
| P22.2 | Coverage targets | Not implemented | No coverage tool; §4 G-26 |
| P22.3 | 1M-row performance | Not implemented | 100k only; §4 G-27 |
| P22.4 | Detector metrics vs recorded baselines | Partial | P8 baseline; P7 / P12–P14 exact expectations; P9 has no target (G-04) |
| P22.5 | Detached runs survive a reboot | Not implemented | They survive a closed terminal only; documented |

### Optional and data-gated phases (P17–P19)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| P17 | Investigation assistant answering from API data only | Partial (optional) | A keyword assistant exists since P9; it answers only from API responses (Node harness A6 / A8). P17 itself is not built: no config-driven threshold text and no reproducibility record |
| P18 | Monte Carlo cash flow | Deferred (data-gated) | Shows "Not available yet", which is P18's own no-data acceptance; browser A5 |
| P19 | Benchmarking | Deferred (data-gated) | Shows "Not available" without a source (P19: hidden); browser A5 |

**Are P17–P19 mandatory?** No.
- `requirements.md` ranks R18, R19 and R20 as **COULD**.
- The problem statement lists Monte Carlo and benchmarking only as "brownie points", and its note makes them
  optional and data-dependent.
- The roadmap labels P17 optional and P18 / P19 data-gated.
- No other project document makes them mandatory.
- The data P18 and P19 need does not exist: no cash-flow import and no genuine benchmark source.

## 4. Incomplete items: impact, class and action

| ID | Phase | Requirement | User impact | Class | Dependency | Recommended action | Blocks release |
|---|---|---|---|---|---|---|---|
| G-01 | P3 | Title rows above the header | Such files are refused with a clear error; the user removes the rows | 3 | — | Header-row selection in the mapping step | No |
| G-02 | P5 | Rings of 5–6 parties | Longer synthetic rings untested; the detector searches up to 5 parties | 3 | — | Add k = 5 / 6 scenarios with the next synthetic revision | No |
| G-03 | P5 | Synthetic banner in the UI | File and case names already say SYNTHETIC | 3 | — | Banner in P23 / P24 polish | No |
| G-04 | P9 | Recall target | P9 recall is reported as a diagnostic, without a pass / fail target | 3 | A target agreed **before** measuring | Agree a target from review capacity, then add a test | No |
| G-05 | P9 | Case calendar and approval limits in the UI | `structuring` and `is_holiday` are unavailable unless `--config` supplies them; the UI says so | 3 | Case-configuration API | Case-settings endpoint and form | No |
| G-06 | P12 / P15 | Analyst decisions feed back into merging and matching | Accepted links stay assessments, so the user re-applies them mentally | 3 | Validated change to the P12 and P14 engines | Separate phase with its own regression evidence | No |
| G-07 | P14 | Reconciliation exceptions feed P9 | Exceptions appear as reconciliation findings, not in P9 scores | 3 | Controlled P9 change and recalibration | Separate validated phase; P9 scores and thresholds unchanged here | No |
| G-08 | P14 | Non-contiguous / multi-counterparty grouping | Some batched payments stay unmatched for review | 3 | — | Extend grouping, keeping deterministic tie rules | No |
| G-09 | P14 | Aging | No age buckets for open items | 3 | — | Aging from the run date | No |
| G-10 | P14 | FX | Cross-currency pairs are not matched; currencies are never mixed | 3 | Rate source (offline) | Analyst-supplied rate table | No |
| G-11 | P14 | Different reference kinds | Fewer reference matches | 3 | — | Reference-kind mapping | No |
| G-12 | P14 | Scope proposals without masters | The analyst enters scopes | 3 | Group-company master | — | No |
| G-13 | P14 | Recorded runs after input changes | Old row-level results are not shown, only the recorded summary and hash. Reports mark them superseded | 3 | Row storage | Optional result-row persistence | No |
| G-14 | P14b | More statement layouts | Unrecognised layouts give `layout_not_recognised`; no data is guessed | 3 | Real samples | New layout strategies with fixtures | No |
| G-15 | P14b | Encrypted PDFs | Refused; the user supplies a decrypted copy | 3 | — | Password prompt (never stored) | No |
| G-16 | P14b | Dr/Cr-signed balance checks | Not evaluated, and stated so | 3 | — | — | No |
| G-17 | P14b / P20 / P21 | Hard memory cap for the OCR worker | A huge legitimate file can use much memory (time, page and pixel limits exist) | 3 | Windows job objects | Job-object memory limit | No |
| G-18 | P15 / P16 | Run and report comparison | Not available | 3 | Persisted run results (G-13) | Comparison view over registers | No |
| G-19 | P15 | Multi-user roles and approval | Single local identity; anyone with the token has full access | 3 | Identity model | Out of scope for a single-auditor laptop tool; document | No |
| G-20 | P15 | Correcting more OCR fields after confirmation | Re-extract or exclude instead | 3 | — | — | No |
| G-21 | P16 | PDF / DOCX file (R15, SHOULD) | The HTML report prints to PDF from the browser; no hashed PDF artefact | 3 | A PDF library (new dependency, offline wheel) | Decide in P23 / P24 whether to add one (e.g. ReportLab) | No |
| G-22 | P16 | Memo sign-off | Memos are drafts | 3 | Identity model (G-19) | — | No |
| G-23 | P16 | Report-specific exclusions | Only dismissal excludes a finding | 3 | — | — | No |
| G-24 | P20 | Truncation detection | Needs the user to record the head anchor outside the database | 3 | — | Document the anchor routine in P23 | No |
| G-25 | P21 | Standalone executable | Requires CPython 3.12 on the laptop | 3 | PyInstaller (new dependency) | Optional | No |
| G-26 | P22 | Coverage measurement | Unknown percentage | 3 | `coverage` / `pytest-cov` (new dependency) | Optional dev dependency | No |
| G-27 | P22 | 1M-row tests | Behaviour at 1M rows is unknown (the ingest row limit applies) | 3 | Time budget | Add an opt-in long test | No |

No incomplete item is class 1. Class 2 had three items, all fixed (§5).

## 5. Gaps fixed before P23

### 5.1 Entity similarity matrix (R6 MUST; P12 acceptance) — class 2

- **Why essential.** The PS asks for "an interactive similarity matrix showing matched pairs … along with shared
  payment patterns and risk scores". P12's acceptance requires "the matrix UI shows per-feature evidence and shared
  payment patterns". The API existed; the UI had only an identity badge in the counterparty table. P24 needs every
  PS MUST demonstrably met.
- **Change.** `frontend/js/similarity-ui.js` (new), a card in the Relationships tab (`index.html`) and CSS:
  - **Read-only, API only:** `GET …/resolution`, `…/resolution/pairs`, `…/resolution/pairs/{id}` and the
    dataset (for the case ID).
  - **Matrix:** records × records; a cell is the API name similarity, coloured by the P12 status. A blank cell is
    explicitly *not* a similarity of zero.
  - **Table and filters:** pair table with status filter, name search and paging.
  - **Pair detail:** status, rule, suggested action, evidence score (labelled as identity evidence, not a fraud
    probability), name components, identifiers, address, evidence, conflicts, shared payment patterns and records.
  - **Loads lazily** when the tab is open, so other tabs do not wait for it.
  - **No change** to scores, statuses or merging.
- **Tests.**
  - Browser A15 (3 checks):
    - the matrix cells and pair rows equal the API;
    - the masters are imported with the documented CLI command into the running server's workspace;
    - the pair detail shows the API's explanation, evidence, conflicts and behaviour;
    - the status filter equals the API.
  - Browser A3 runs the module on the hostile CSV (no injected elements); A4 confirms 0 CSP violations.
  - `test_pre_p23_gaps.py` checks the module is read-only.
  - The Node harness and `test_frontend_static.py` scan the new file.
- **Not claimed.** "Per-pair risk" is the P12 status and evidence score. No fused pair-risk score exists. Analyst
  confirm / reject stays in investigations.

### 5.2 Forensic invariants that also hold under `python -O` — class 2 (integrity)

- **Why essential.** Python removes `assert` statements under `-O` (or `PYTHONOPTIMIZE`). Three of them guarded
  forensic totals:
  - two in reconciliation: every row in exactly one result, and status counts covering every row;
  - one in reports: each linked transaction counted exactly once.

  Under `-O` a violation would have produced wrong totals silently.
- **Change.**
  - `ledgerspy/errors.py`: `InvariantError` (HTTP 500, code `internal_invariant_failed`) and `require()`.
  - `reconciliation/engine.py` and `reports/build.py` use `require()`.
  - The two import-time vocabulary checks (`api/routes/graph.py`, `investigations/refs.py`) were converted the
    same way.
  - No result changes: identical inputs give identical hashes (§8).
- **Tests** (`test_pre_p23_gaps.py`):
  - no `assert` statement remains anywhere in `ledgerspy/` (AST scan);
  - `require` raises with its details;
  - a row taken twice is refused in a fresh interpreter **with and without `-O`**. The same script proves `-O`
    really strips a plain `assert` there.

### 5.3 Master-data import in the bundle — class 2 (documented workflow broken)

- **Why essential.**
  - `installation.md` §4 told the user to run `.venv\Scripts\python -m ledgerspy entities import-master …`
    from the bundle folder.
  - Without `--workspace`, the CLI uses `workspace.path: workspace`, relative to the current folder. That is not
    `%LOCALAPPDATA%\LedgerSpy\workspace`, which `ledgerspy.cmd` serves.
  - The command therefore failed with "no case …" and created an empty `workspace\` inside the application
    folder.
  - The UI never showed the case ID the command needs.
  - Without masters, employee–vendor evidence (R6 / R9) is unavailable.
- **Change.** No CLI behaviour changed.
  - `installation.md` §4 gives the exact command with `--workspace "%LOCALAPPDATA%\LedgerSpy\workspace"`, the case
    source and `--use-suggested` (each P5 master was checked to import with it).
  - The similarity card names the case ID and the command.
- **Tests.**
  - `test_pre_p23_gaps.py`:
    - the documented command names the launcher's default workspace (this test failed on the old text);
    - without `--workspace`, from another folder, the case is not found and nothing reaches the served workspace;
    - with it, the import lands and lists.
  - Browser A15 runs the documented command, with the case ID read from the UI, against a live server.

### Files changed

| Change | Files |
|---|---|
| Code | `frontend/js/similarity-ui.js` (new), `frontend/index.html`, `frontend/css/main.css`, `ledgerspy/errors.py`, `ledgerspy/reconciliation/engine.py`, `ledgerspy/reports/build.py`, `ledgerspy/api/routes/graph.py`, `ledgerspy/investigations/refs.py` |
| Tests | `tests/backend/test_pre_p23_gaps.py` (new). `tests/backend/test_verify_p22.py`: the inventory test now covers P0–P22 and the audit file, and no longer exempts its own file. `tests/frontend/browser_check.py`: A15 |
| Docs | `docs/installation.md` §4, `docs/entity-resolution.md` (UI), `docs/acceptance-inventory.json` (P22 added; P12 gap closed; `closed_before_p23` notes), `docs/roadmap.md` (P12 note), `README.md`, this file |

Historical acceptance records (`p*-acceptance.md`) are unchanged. Statements in them that are no longer true are
listed in the inventory's `closed_before_p23`.

## 6. Environment-only verification (the user's machine)

| ID | Item | Why it cannot be closed here |
|---|---|---|
| E-01 | Fresh install and full demo on the **target laptop with networking disabled** (P21 / P24) | Disabling the adapter changes system settings. The automated check isolates the installation's processes, not the machine |
| E-02 | Offline behaviour on the user's actual laptop | Only this development machine was used |
| E-03 | `pip-audit` of `requirements.lock` | Needs network access; a build-machine step |
| E-04 | Non-Windows or ARM targets | Only Windows 11 x64 is available and verified |
| E-05 | Real bank-statement PDFs | No real statements are available; layouts are validated on synthetic and fixture PDFs only |
| E-06 | Non-English OCR and handwriting | Needs language data and samples |
| E-07 | Tesseract on the target laptop | A separate local install; `doctor.cmd` reports it. Without it, OCR tests are skipped and listed as skipped |
| E-08 | A real public ledger end to end | P24 scope |

## 7. Optional and data-gated

P17, P18 and P19 are not mandatory (§3). P18 and P19 already meet their own "no data" acceptance: the UI
states "not available" and computes nothing. No P17–P19 feature was implemented to make the roadmap look
finished.

## 8. Verification (final code)

### 8.1 Consolidated run

Command: `.venv\Scripts\python tools\verify.py --offline --detach`, followed with
`tools\verify.py status verify-results\20261011T122115Z --wait`.

Run `verify-results/20261011T122115Z` (git-ignored):
- **Outcome:** **PASSED, exit 0**, `complete: true`.
- **Time:** started 2026-10-11T12:21:16Z, ended 13:05:08Z.
- **Environment:** Python 3.12.4, Windows 11.
- **Source:** `36c9e72` plus the uncommitted changes of this audit. The only file modified after the start is this
  document (text only).

| Check | Kind | Status | Counts | Cross-check against the tool's own report |
|---|---|---|---|---|
| `ruff-lint` | required | passed | 0 findings | `All checks passed!` |
| `ruff-format` | required | passed | 273 files | `273 files already formatted` |
| `backend` | required | passed | **1240 passed, 0 failed, 0 errors, 0 skipped** | pytest: `1240 passed in 2396.59s`; JUnit `tests=1240 failures=0 errors=0 skipped=0` (P22: 1233, plus 7 new) |
| `node-harness` | required | passed | 13 / 13 | `13 passed, 0 failed` |
| `browser` | required | passed | 25 / 25 (P22: 22, plus A15 × 3) | `25 passed, 0 failed`; A4: 235 requests, 0 external, 0 CSP violations; A9: no JS errors |
| `p4-panel` | required | passed | 6 / 6 | `6 passed, 0 failed` |
| `package` | required | passed | 8 / 8 | 8 PASS lines. The P22 bundle (`36c9e72`) matches its commit file by file (249 files); manifest and SHA-256 valid; reproducibility not checked (dirty tree, as designed) |
| `offline-bundle` | optional | passed | 26 / 26 | `26 passed, 0 failed`; temporary install removed |

**No OCR test was skipped:** a local Tesseract is present on this machine.

### 8.2 Targeted checks of the changed areas

| Check | Result |
|---|---|
| `tests/backend/test_pre_p23_gaps.py` | 7 passed (in the run). The documentation test failed on the old `installation.md` text before the fix |
| Browser check, standalone run before the final run | 25 passed. The first attempt aborted in the existing chatbot step (A7) when the matrix loaded eagerly and slowed the server. The module now loads only when its tab is opened; the existing check was not changed |
| **P9 / P12 / P14 identity**: the committed `36c9e72` (temporary git worktree) against the working tree, with the same script on the P5 company and PaySim, IDs made deterministic | See below |
| Migrations | None added or changed (schema v9); no workspace format change |
| Git | No file outside the listed ones changed. No workspace, database, `dist/` or `verify-results/` content is tracked |

**Identity comparison detail.**
- **Control first.** The engines' `result_sha256` values include the random case / dataset ULIDs, the temporary
  workspace path (config hash) and the integrity run's timestamp. A control (the same code run twice) therefore
  differs too. The comparison was repeated with a deterministic ID generator.
- **P9 scores and thresholds unchanged.** Every P9 row (score, tier, contributions) and every entity is identical
  between HEAD and the working tree, for:
  - the synthetic ledger without masters;
  - the synthetic ledger with masters;
  - PaySim.

  Only the run-specific summary fields above differ, exactly as in the control.
- **Reconciliation and resolution identical.** The run's `result_sha256` and summary, and the P12
  `resolution_sha256`, are identical between HEAD and the working tree.

### 8.3 Not verified here

Everything in §6: the network-disabled target laptop, `pip-audit`, other operating systems, real statements and
other OCR languages. LedgerSpy is **not** claimed to be complete, secure, or offline-verified on the user's laptop.

## 9. Recommendation

**Ready for P23.**
- No release blocker remains.
- The three essential gaps are fixed and tested.
- Every other incomplete item is classified, documented and stated by the product where it applies (§4, §6, §7).

P23 should document them as limitations, not as features. In particular:
- reconciliation exceptions are not in P9 scores;
- there is no PDF artefact;
- memos are drafts;
- there is a single user;
- the master import is CLI-only;
- the offline test on the user's laptop is still the user's step (P24).

After this commit, rebuild the release bundle from the clean commit (`installation.md` is part of it) and validate
it with `tools/verify.py package-check`.
