# P23 — Final User and Operations Documentation: Acceptance Record

Date: 2026-10-12 · Scope: [`roadmap.md`](roadmap.md) §P23 · Starting point: `f305f99` (pre-P23 gap audit), clean
tree. P24 is **not** started.

## 1. What was delivered

| Document | Status | Content |
|---|---|---|
| [`README.md`](../README.md) | Rewritten top | Product overview: purpose, supported workflow, automated indicators vs analyst conclusions, import and report formats, main limitations, a "Start here" table. The phase-by-phase record is kept, unchanged in substance, as "Development history". Two stale lines fixed: the Python version and the inventory range |
| [`installation.md`](installation.md) | Extended | What is and is not bundled; repeat installation; manifest and zip hash verification. The offline claim is restated precisely (process-level guard on the development machine, not a network-disabled target laptop) |
| [`user-guide.md`](user-guide.md) | **New** | The auditor workflow: start, import, mapping, data quality, analysis, entities and graph, reconciliation, PDF / OCR review, investigations, reports. Also how to read scores, tiers, ambiguous matches, stale runs and statuses; draft memo vs approval; evidence handling |
| [`operations.md`](operations.md) | **New** | `doctor`, start / stop, port and workspace conflicts, logs, workspace contents, backup / restore, migrations and their backups, the audit anchor procedure and its limits, recovery from interrupted operations, missing OCR, release verification and exit codes, package hash verification, keeping earlier releases |
| [`security.md`](security.md) | **New** | Deployment assumptions, the P20 threat model in brief, controls vs limits, audit-trail detection table, known limits, deferred items from the inventory |
| [`configuration.md`](configuration.md) | **New** (roadmap: "config reference") | How the file is loaded (`--config` replaces, not merges), strict validation, hashing and its effect on recorded runs, case-specific settings, sections |
| [`developer-guide.md`](developer-guide.md) | **New** (roadmap: "developer guide") | Setup, layout, the rules the tests enforce, testing, building and validating a release |
| [`verification.md`](verification.md) | One line | An approximate test count replaced by "its current count is in the run's report" |
| [`roadmap.md`](roadmap.md), [`acceptance-inventory.json`](acceptance-inventory.json) | Updated | P23 marked done with an as-built note and its gap; inventory P0–P23 |
| `tests/backend/test_docs_p23.py` | **New** | The consistency checks of §3 |
| `tests/backend/test_verify_p22.py` | One line | The inventory test now expects P0–P23 |
| `pyproject.toml` | Comment only | The Ruff `S101` comment no longer says asserts are used for internal invariants (they are not since `f305f99`) |

**Unchanged:**
- **Methodology and model cards:** `forensics.md`, `anomaly-model-card.md`, `risk-engine.md`,
  `supervised-model-card.md`, `model-lifecycle.md`, `entity-resolution.md`, `relationship-graph.md`,
  `reconciliation.md`, `pdf-extraction.md`, `investigations.md`, `reporting.md`.
- **Data provenance:** `data-strategy.md`, `data/README.md`.
- **Phase acceptance records:** all of them; they are referenced, not replaced.
- **Application code:** no change. The only non-documentation edits are the tests above and the `pyproject.toml`
  comment.

## 2. How the content was checked against the implementation

Every statement in the new guides was taken from the code or confirmed by running it.

**UI behaviour**
- **UI flows and labels:** read from `frontend/js/*.js` and `index.html`, including the button texts, status
  vocabularies and the order of steps.
- **Recon statuses** (`not_applicable` = outside every scope) were taken from `reconciliation.md` and the
  engine.
- **Report types, formats and the draft notice** come from `ledgerspy/reports/model.py` and `/api/reports/vocabulary`
  (`approval_supported: false`).
- **Investigation statuses, transitions, outcomes and decisions** come from `ledgerspy/investigations/model.py`.

**Commands and outputs**
- **CLI:** every command and option was checked with `--help`.
- **`doctor`:** run on a fresh and on a populated workspace (all ten check lines, `RESULT: ready`, exit 0). With an
  invalid configuration it gives `FAIL configuration` and exit 1.
- **`audit verify` / `evidence verify`:** run on a populated workspace (`14 entries verified; 53 triggers intact;
  head seq 14 hash …`; exit 2 when no database).
- **Conflicts:** a second server on the same workspace gives exit 3 with "another LedgerSpy server is already
  using the workspace …". A busy port gives exit 3 with "127.0.0.1:8791 is already in use …". `doctor` reports
  both as `WARN`.
- **Audit anchor procedure** (`operations.md` §6), run as written in PowerShell against a live server:
  - the correct anchor gives `ok=True matches=True`;
  - a wrong hash gives "the chain was rewritten up to it";
  - an unknown sequence gives "the log was truncated below it";
  - no token gives HTTP 401.

  Confirmed that the anchor exists only in the API: no CLI option, no UI control.

**Configuration and package**
- **Configuration:** `--config` replaces the default file. A partial file is refused (19 validation errors),
  and a full copy with the documented holidays and approval-threshold example loads and passes `doctor --config`.
  `ledgerspy.cmd` and `doctor.cmd` pass their arguments on.
- **Manifest check:** the PowerShell snippet in `operations.md` §10 was run on an unzipped copy of the release
  bundle. It printed "OK"; after one byte was appended to `README.md` it printed `MISMATCH: README.md`.
- **What the bundle contains:** from `build_bundle.py`'s allow-list and an unzipped bundle. `tools\` and `tests\`
  are not shipped, so the release verification is documented as a source-repository step.

**Recovery and logs**
- **Interrupted operations:**
  - upload staging deletes its `.part` file on any error or cancellation, and only a hard kill can leave one;
  - an evidence file moved but not committed causes `evidence_conflict` on re-import, and `evidence verify`
    does not list unregistered files;
  - nothing is registered for an unfinished PDF extraction.
- **Logs:** there is no log file (no logging handler; `access_log=False`). Output is the console window only.

## 3. Automated consistency checks (`tests/backend/test_docs_p23.py`)

| Check | Scope |
|---|---|
| Relative links resolve; same-file anchors exist | README and every `docs/*.md` |
| Every `ledgerspy …` command and `--option` in code spans or blocks exists in `ledgerspy.cli.build_parser()`; `ledgerspy.cmd` / `doctor.cmd` options exist on `serve` / `doctor` | User-facing documents (README, installation, user guide, operations, security, configuration, developer guide, verification) |
| `tools/verify.py` subcommands and options exist; `build_bundle.py`, `make_lock.py`, `offline_check.py` options exist | User-facing documents |
| Every `/api/…` path is a registered route; a `…`-suffixed family has an existing stem | User-facing documents, against `app.openapi()` (all routes) |
| Every repository path named in code exists | User-facing documents |
| The documented bundle file name equals the builder's pattern and the `pyproject.toml` version | User-facing documents |
| The 27 UI labels the user guide tells the reader to use exist verbatim in the frontend, and each is used in the guide | `docs/user-guide.md`, `frontend/` |
| Report types and formats match the report model. PDF / DOCX are absent from the model and stated as absent | README, user guide, security notes |
| No approval or sign-off of memos is described as possible; the API says `approval_supported: false` | User-facing documents |
| No machine-specific user path or session token (the bundle builder's own scan) | README and every `docs/*.md` |

**The tests found these defects, now fixed:**
- **The README repository layout** named `frontend/harness.js` under `tests/` ambiguously.
- **Two problems in the first draft of the tests themselves,** found when they first ran:
  - the route list missed included routers;
  - `::` batch comments in `verification.md` were read as arguments.

**The tests can fail.** An unknown option (`--bogus`) and a misspelled subcommand are reported.

## 4. Verification (final tree)

`.venv\Scripts\python tools\verify.py --offline --detach`, followed with
`tools\verify.py status verify-results\20261011T204441Z --wait`.

Run `verify-results/20261011T204441Z` (git-ignored): **PASSED, exit 0**, `complete: true`.
- **Time:** started 2026-10-11T20:44:42Z, ended 21:15:57Z.
- **Source:** `f305f99` plus the P23 changes.
- **Edits after the start:** none to any file. This section was filled in afterwards.

| Check | Kind | Status | Counts | The tool's own report |
|---|---|---|---|---|
| `ruff-lint` | required | passed | 0 findings | `All checks passed!` |
| `ruff-format` | required | passed | 280 files | `280 files already formatted` |
| `backend` | required | passed | **1347 passed, 0 failed, 0 errors, 0 skipped** | pytest `1347 passed in 1527.65s`; JUnit `tests=1347 failures=0 errors=0 skipped=0` (1240 before P23, plus the 107 of `test_docs_p23.py`) |
| `node-harness` | required | passed | 13 / 13 | `13 passed, 0 failed` |
| `browser` | required | passed | 25 / 25 | `25 passed, 0 failed` (0 external requests, 0 CSP violations) |
| `p4-panel` | required | passed | 6 / 6 | `6 passed, 0 failed` |
| `package` | required | passed | 8 / 8 | 8 PASS, 0 FAIL. The `f305f99` bundle matches its commit file by file. Reproducibility is not checked on a dirty tree, as designed |
| `offline-bundle` | optional | passed | 26 / 26 | `26 passed, 0 failed` |

No OCR test was skipped, because Tesseract is installed on this machine. The release bundle is rebuilt from the
P23 commit afterwards, because the bundle ships `docs\`, `README.md` and `pyproject.toml`. The rebuild is
validated with `package-check`.

## 5. Remaining documentation gaps

- **No independent walkthrough.** The roadmap's acceptance, "a new developer can install offline and run the
  demo case from the docs alone", was checked by review and the automated checks. Nobody but the developer has
  followed the documents, and no install on a network-disabled machine was done (P24).
- **No screenshots.** The user guide describes the UI in text; real screenshots would be easier to follow.
- **Master-data import is documented as a command line.** The UI has no master upload; the guide cannot make that
  easier than the product is.
- **Not covered by the automated checks:**
  - **Methodology documents and acceptance records:** their links and the absence of machine paths are
    checked; their commands are not. They describe phase-time behaviour.
  - **UI labels:** checked only for the user guide's 27 listed controls, not for every UI string quoted elsewhere.
  - **API response fields:** names quoted in the documents (e.g. `anchor.matches`) were checked by hand,
    not by the tests.
- **Translations:** none; the documentation is English only.
- **Supported platform:** the documents describe the verified platform only (Windows x64, Python 3.12). Other
  systems are stated as unverified, not documented.
