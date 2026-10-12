# P24 — Final End-to-End Acceptance Audit and Release Decision

Date: 2026-10-12 · Scope: [`roadmap.md`](roadmap.md) §P24 · No further phase is started.

## 1. Scope and acceptance criteria

The roadmap's P24 criteria:
- **Objective.** On a network-disabled laptop, run the synthetic company and a real public ledger end to end
  (integrity → analysis → investigation → confirmed findings → memo / PDF). Verify the audit log and reproduce
  the run from its manifest.
- **Acceptance.** All PS MUST requirements are demonstrably met with real computation; every displayed number is
  traceable; the reproduced run is identical.

What could be verified here, and what could not:

| Criterion | Verifiable here? |
|---|---|
| Synthetic company and a public ledger end to end; audit log; reproduction | **Yes**, on the **installed release bundle** (§4) |
| "Memo / PDF" | Memo yes (always a draft). **No PDF exists by design** (JSON / HTML / Markdown only); stated, not built |
| "On a network-disabled laptop" | **No.** This machine's adapter was not disabled (that would change system settings). Every installed process ran under a network guard with `netstat` sampling, which is evidence about the application, not about the target laptop |

The starting claims (`7123aaf`; run `20261011T204441Z`; bundle `aa0f6b77…6ca066`) were checked against the
repository and the files before being relied on (§2).

## 2. Commits and working tree

| | Commit | Tree |
|---|---|---|
| Start | `7123aaf` (P23), branch `main` | clean. `dist\ledgerspy-0.2.0-win_amd64-py312-offline.zip` SHA-256 `aa0f6b774649fca4cad240266ce95a12f78c8e0163969d7c54da7bcff16ca066`, `BUILD-INFO.json` source `7123aaf`, clean: as reported |
| Fixes | `fc19b21` "P24: Fix busy-database commit handling and the reports' synthetic flag" | clean after commit |
| Final | the commit that adds this record (documentation and the inventory test only) | clean after commit |

**Kept, not overwritten:** earlier bundles are preserved by `build_bundle.py` (`…aa0f6b774649.zip`,
`…24975a1116e4.zip`, `…1b93bc571ff4.zip`, `…b51dedc4dc1e.zip`). Nothing in the user's workspace was touched: every
run used fresh scratch folders.

## 3. Defects found and fixed (release blockers)

| # | Defect | How it was found | Class | Fix | Regression tests |
|---|---|---|---|---|---|
| D1 | A refused `COMMIT` ("database is locked") left the transaction **open**, and the evidence file already moved into `evidence\` remained **without its record**. Every retry of that file was then refused with `evidence_conflict` until the evidence folder was repaired by hand | Required check failed: verification run `20261011T213841Z`, browser A15 timed out after the documented CLI master import. Reproduced live: 2 of 12 documented imports failed while the server built the graph (SQLite default 5 s busy timeout). Shown deterministically: after the failure `in_transaction` was True; after close the file was orphaned; the retry gave `evidence_conflict` | **A** (evidence-store integrity; documented workflow fails) | `transaction()` rolls back a refused COMMIT. The evidence store registers an unregistered file whose bytes match the import (the name is its SHA-256); different bytes stay refused. Connections wait 30 s for a lock. The CLI prints "the workspace database stayed busy … rolled back" instead of a traceback. Under the same load afterwards: **12 of 12** imports succeeded, no orphan | `tests/backend/test_p24_fixes.py` (5 tests; **4 fail on the old code**, the unchanged-behaviour test passes) |
| D2 | Reports stated a dataset was **not synthetic** (`"synthetic": false`, HTML "no") when its source declares nothing (every generic ledger and bank statement, including the synthetic demo company) | P24 API probe; E2E W12c failed on the `7123aaf` bundle (`[False, False]`) | **A** (an unsupported factual claim in audit report artefacts) | JSON keeps `null`; HTML shows "not declared". The memo's "flagged synthetic" label is unchanged | `test_reports.py::test_undeclared_synthetic_status_is_reported_as_unknown_not_false` (fails on the old code: `False is None`) |

**Effect on results.** Neither fix changes P9 scores, tiers, entity resolution or reconciliation. The E2E run on
the fixed bundle gives the same tier counts and reconciliation results as on the `7123aaf` bundle (§4). The
pre-P23 identity comparison method was not needed: no engine code changed.

## 4. End-to-end workflow matrix (installed release bundle)

**How it was run.**
- **Tool:** `tools/e2e_acceptance.py --bundle dist\ledgerspy-0.2.0-win_amd64-py312-offline.zip --work <empty>`.
- **Bundle:** the candidate built from `fc19b21`, SHA-256
  `1e580a9a6bb589c9fdad4dc95beec6c6c08932ed1071b133a17544640e9cb44c`.
- **Procedure:** it installs the bundle by the documented procedure, starts it with `ledgerspy.cmd`, and drives
  the API the UI uses, plus the documented CLI.
- **Expected values:** derived from the fixture files (rows, amounts, SHA-256) or from the evaluation-only P5
  ground truth.

**Result: 30 PASS, 0 FAIL, 1 NOT TESTED.**

| ID | Stage / action | Fixture | Expected | Actual | Result |
|---|---|---|---|---|---|
| I1 | Unzip; every file vs `MANIFEST.sha256` | bundle | all match, none unlisted | 258 listed, 0 mismatched, 0 unlisted | PASS |
| I2 | `install.cmd` (offline wheelhouse, `--require-hashes`) | bundle | exit 0, doctor ready | exit 0, `RESULT: ready` | PASS |
| W1 | `ledgerspy.cmd`; open the printed link; page without token; API without token | — | UI 200; locked page 401; API 401 | 200; 401 with "LedgerSpy is locked"; 401 | PASS |
| W2 | New engagement case | — | a case | `C-…` | PASS |
| W3 | Import ledger, confirm mapping | `SYNTHETIC_ledger.csv` | 7,271 rows; evidence SHA-256 = file SHA-256; stored copy equal | 7,271 (canonical 7,271); equal | PASS |
| W4 | Documented CLI master import with `--workspace`, server running | vendor / employee / customer masters | exit 0; records = CSV rows | exit 0 ×3; 99 / 37 / 26 records | PASS |
| W4b | The same command **without** `--workspace` | vendor master | refused, served workspace unchanged | exit 1 "no case …" | PASS |
| W5 | Integrity check | ledger | rows = 7,271; issues = Σ by check = listed; rows cited exist; unavailable checks give reasons | 20 issues (Σ 20, listed 20); readiness 99.9; `balance_continuity`, `out_of_period` not available with reasons | PASS |
| W6 | Analysis (features → rules → anomaly → P9), every row paged | ledger | each file row once; tier counts = rows by tier; total amount = CSV Σ 1,496,022,093.79; high+critical amount = Σ of those rows; tiers obey 25 / 50+2 / 75+3 | 7,271 rows; critical 2, high 53, medium 178, low 7,038; totals equal; 0 tier-rule violations | PASS |
| W7 | Every high / critical explanation | ledger | contributions = score (±0.01); every contributor with evidence and a sentence; "not a probability" | 55 rows; 0 mismatches; 0 without evidence | PASS |
| W8 | Entity resolution and conflicting pairs | ledger + masters | master records = imported rows; conflicts found, never merged | 162 master records; 11 conflicting pairs; 0 merged | PASS |
| W8b | Graph cycles vs ground truth | S-O-01 (3-party), S-O-02 (4-party) | each a `review` cycle with its rows; only these two flagged | both found; 2 of 5 cycles `review`; all chain IDs in the file | PASS |
| W9 | Statement import + reconciliation (scopes LVE / LVC) | `SYNTHETIC_bank_statement.csv` | 7,267 lines; every row exactly once per side; Σ amounts by status = file totals | equal on both sides (ledger 1,496,022,093.79; bank 1,495,966,911.01) | PASS |
| W9b | Every S-S scenario row's status | ground truth S-S-01…07 | unmatched ledger / unmatched bank / partial (delay, fees, payee with masters) / grouped | 0 mismatches (partial 8 = 4 + 3 + 1) | PASS |
| W9c | Ambiguous results | same run | ≥ 2 candidates, none chosen | 3 results, 4 candidates each | PASS |
| W10 | PDF with a scanned page; confirm before review | generated P5 PDF | OCR used; OCR lines review-required; early confirm refused | OCR; 12 of 12 required; HTTP 409 `pdf_review_pending` | PASS |
| W10b | Decide every line; confirm | same | accepted | HTTP 200 | PASS |
| W10c | Text PDF reconciled; correction of a confirmed line | generated PDF (40 rows) | balance correction refused; reference correction → transformation → run stale | 409 `not_correctable_after_confirmation`; run invalidated and stale; its rows 409 | PASS |
| W11 | Investigation: links, hostile note, decision, invalid transition, conclusion | top P9 row + a partial result | invalid transition refused; every event audit-referenced; P9 unchanged | 409; 7 events with audit seq; P9 `result_sha256` unchanged | PASS |
| W12 | Four report types × JSON / HTML / MD | investigation + run | downloaded SHA-256 = recorded; stored | 16 of 16 | PASS |
| W12b | Report content vs sources | reports | recon counts = run; linked amount = CSV Amount; memo DRAFT; note escaped | all equal / true | PASS |
| W12c | Report "synthetic" flag | generic ledger | unknown → null (D2) | `[None, None]` (failed on the old bundle: `[False, False]`) | PASS |
| W13 | `audit verify`, `evidence verify` (installed CLI); API anchor | P24 workspace | exit 0; anchor matches | exit 0 / exit 0; anchor seq 51 matches | PASS |
| W13b | Tamper a **copy**: one byte on evidence; drop a trigger and edit a note | copy | both detected | evidence exit 1 "hash mismatch"; audit exit 1 naming the trigger | PASS |
| W14 | Report on the invalidated run | stale run | superseded, not current | `run_state` stale, with reason | PASS |
| W15 | Empty / corrupt / unmappable files, unconfirmed analysis, unknown IDs, invalid report / run requests | hand-made | explicit 4xx code, no result | `empty_file`, `xlsx_invalid`, `mapping_invalid`, `not_canonicalised`, `dataset_not_found`, `investigation_required`, … | PASS |
| W16 | Ctrl+Break; restart on the same workspace; recompute | P24 workspace | P9 `result_sha256` and run result identical | identical | PASS |
| W17 | Public ledger end to end | `paysim_sample_10k.csv` (public simulation) | 10,127 rows; labels split off; no high / critical (P9 record); unavailable detectors stated | 10,127; `isFraud`, `isFlaggedFraud` split; 0 high / critical; 7 unavailable | PASS |
| S1 | Graceful stop | — | exit 0 | 0 | PASS |
| N1 | Network guard in every installed Python process; `netstat` of the installation | whole run | no attempt, no foreign connection | guard log empty; 0 foreign in 67 samples | PASS |
| N2 | The same on a machine with networking **disabled** | target laptop | — | **not performed** | NOT TESTED |

**UI (automated and manual).**
- **Automated:** the browser check (A1–A15, 25 checks) drives the same workflow in Chromium against the
  development server. Hero, table, explanation, graph, reconciliation, PDF review, investigation, reports and
  similarity matrix all equal the API. There are no JavaScript errors, no CSP violations and no non-loopback
  requests.
- **Manual (packaged app):** the installed bundle was opened on the E2E workspace in the in-app browser.
  - Analysing the synthetic ledger showed tiers 2 / 53 / 178 / 7,038, equal to `GET …/risk`, with the "not a
    probability" banner.
  - The similarity matrix showed 20 cells and 16 pair rows, equal to the API's 16 pairs.
  - The case-ID hint confirmed the imported masters, and the graph drew 154 nodes.
  - There were no console errors, and all 49 requests went to `127.0.0.1:8799`.

**First E2E pass on the `7123aaf` bundle.**
- **Result:** 27 PASS / 3 FAIL.
  - W12c failed: defect D2.
  - W1 and W8b failed because the script's expectations were wrong:
    - the page without a token is the documented locked page (401);
    - S-O-01 is a 3-party ring whose fourth ground-truth row is the intercompany mirror booking
      (`p13-acceptance.md`).
- **Corrections:** both were corrected to the documented behaviour, not loosened. W1 now also requires the locked
  page, and W8b requires exactly two flagged cycles.

## 5. Consolidated verification

| Run | Tree | Result |
|---|---|---|
| `20261011T213841Z` | fixes in progress (D2 only) | **FAILED (exit 1)**: browser A15 timeout. This was D1 (above); all other required checks passed (backend 1348) |
| `20261011T223526Z` | `fc19b21` content (D1 + D2) | Required checks **passed** (exit 0): Ruff clean (282); backend **1353 passed, 0 failed, 0 errors, 0 skipped**; Node 13/13; browser 25/25 (0 external, 0 CSP); P4 panel 6/6; package 8/8 |
| (final) | this commit | see §5.1 |

**Run `20261011T223526Z` was disturbed by sleep.**
- **What happened:** the machine slept from 22:40:49 to 03:53 UTC (system log, Kernel-Power 42/107). One test
  shows 18,776 s of wall time, against 6.9 s normally.
- **Optional check:** right after the resume, the `offline-bundle` check failed 2 of 26 stop checks (one
  leftover process; "stopped cleanly" missing).
- **Rerun:** standalone on the same bundle it passed 26/26, and on the candidate bundle 26/26 (§6).
- **Afterwards:** keep-awake was requested for the remaining runs.

### 5.1 Final run

`.venv\Scripts\python tools\verify.py --offline --detach`, followed with
`tools\verify.py status verify-results\20261012T042847Z --wait`.

Run `verify-results/20261012T042847Z`: **PASSED, exit 0**, `complete: true`.
- **Time:** 2026-10-12T04:28:48Z to 04:50:54Z, with keep-awake held and no sleep.
- **Source:** `fc19b21` plus this record, the roadmap, the inventory and the README.
- **Edits after the start:** none to any file. This section was filled in afterwards.

| Check | Kind | Result | The tool's own report |
|---|---|---|---|
| `ruff-lint` | required | passed | `All checks passed!` |
| `ruff-format` | required | passed, 283 files | `283 files already formatted` |
| `backend` | required | **1355 passed, 0 failed, 0 errors, 0 skipped** | pytest `1355 passed in 1104.12s`; JUnit `tests=1355 failures=0 errors=0 skipped=0`. P23 had 1347; P24 adds 1 + 5 regression tests and 2 documentation checks for this file |
| `node-harness` | required | 13 / 13 | `13 passed, 0 failed` |
| `browser` | required | 25 / 25 | `25 passed, 0 failed`; A4: 235 requests, 0 external, 0 CSP violations; A9: no errors, no dialogs |
| `p4-panel` | required | 6 / 6 | `6 passed, 0 failed` |
| `package` | required | 8 / 8 | 8 PASS: the `fc19b21` candidate bundle, file by file against its commit. Reproducibility is not checked on a dirty tree (it is checked in §6) |
| `offline-bundle` | optional | 26 / 26 | `26 passed, 0 failed`, on the `fc19b21` candidate bundle |

No OCR test was skipped: Tesseract 5.4 is installed on this machine.

## 6. Package validation and reproducibility

**The tested release candidate** is the bundle built from `fc19b21`:
`dist\ledgerspy-0.2.0-win_amd64-py312-offline.zip`, 15,452,944 bytes, 259 files, SHA-256
**`1e580a9a6bb589c9fdad4dc95beec6c6c08932ed1071b133a17544640e9cb44c`**.

| Check | Result |
|---|---|
| `tools/verify.py package-check` | **9/9 PASS**: one top folder; manifest covers exactly the files; every SHA-256 matches; no tokens / databases / workspaces / caches / ground truth / user paths; required files; clean source commit `fc19b21`; commit exists; every file equals its commit (257 compared, wheels by the lock); **rebuild from HEAD byte-identical** |
| Independent second rebuild into another folder | byte-identical (same SHA-256) |
| Zip contents | `install.cmd`, `ledgerspy.cmd`, `doctor.cmd`, `INSTALL.md`, `README.md`, `MANIFEST.sha256`, `BUILD-INFO.json`, `requirements.lock`, `pyproject.toml`, `config\`, `ledgerspy\`, `frontend\` (11 JS), `docs\` (49), `data\synthetic\reference\` (no ground truth), `wheelhouse\` (19 wheels). **Not present:** tests, tools, packaging scripts, workspaces, databases, `.pyc`, `data\raw\`, ground truth |
| `packaging/offline_check.py` on this bundle | **26/26**: install twice (second reuses `.venv`); doctor ready; no workspace created by install; full feature flow; UI without external request or CSP violation; second server refused; Ctrl+Break stops with no process left and the lock released; restart with data intact; **v8 → v9 upgrade with a read-only backup**, data kept; 0 guarded network attempts; 0 foreign TCP connections (68 samples, 26 processes) |

**The final release bundle** is rebuilt from the final commit, which adds this record. Its hash, and the check
that it differs from the tested candidate only in documentation files, are reported with that commit (§10). The
application code, UI, configuration and wheels are those tested above.

## 7. Security, integrity, data correctness and offline results

| Area | Evidence | Result |
|---|---|---|
| Loopback only; token required; locked page without the token | W1; P20 tests in the backend suite | verified |
| No external requests; no CSP violation; no JS errors | browser A4 / A9; manual packaged-UI check; offline check | verified |
| Evidence integrity | W3 (stored copy = file hash); W13 / W13b (verify passes; a changed byte is detected) | verified |
| Audit chain, trigger integrity, external anchor | W13 (anchor matches); W13b (dropped trigger and edit detected) | verified (tamper-evident, not tamper-proof) |
| Totals traceable to source files | W6, W9, W12b: totals equal the fixture files' sums; every row once | verified |
| Analyst records vs automated outputs | W11 (P9 unchanged after decisions); reports separate origins | verified |
| Stale inputs | W10c, W14 | verified |
| Explicit failure on invalid input | W15; D1's CLI message | verified |
| Reproducibility | W16 (restart: P9 and run hashes identical); second byte-identical rebuild | verified |
| Offline (application) | N1, offline check: 0 attempts, 0 foreign connections | verified on this machine |
| Offline (target laptop, adapter disabled) | — | **NOT TESTED** |
| Dependency vulnerabilities (`pip-audit`) | — | **NOT TESTED** (needs network) |

**PS MUST requirements (R1–R12) with real computation.**

| Requirement | Evidence | Note |
|---|---|---|
| R1 offline / local | N1; offline check | Except the target laptop (N2) |
| R2 CSV / XLSX ingestion | W3, W17, W15 | |
| R3 Integrity dashboard | W5; P4 panel 6/6 | |
| R4 Logged cleaning | W10c (logged transformation) | Plus the P4 tests |
| R5 Benford | backend P7 tests; browser A5 | Charted from data, with sample size and band |
| R6 Fuzzy entities with matrix | W8; A15; manual check | Per-pair risk = P12 status and evidence score |
| R7 Label-free anomalies with scatter | W6; A5 / A6 | |
| R8 Bank reconciliation incl. scanned | W9–W10c; A11 / A12 | Layouts limited (§8) |
| R9 Relational graph | W8b; A10 | Size = exposure, opacity = PageRank |
| R10 Explainable breakdown | W7; A5 | |
| R11 Memo | W12 / W12b | Always a draft |
| R12 Unified risk with bounded volume | W6 | |

## 8. Outstanding issues, classified

**A — Release blockers: none remaining.** D1 and D2 were fixed (§3).

**B — Acceptable for a limited pilot, with explicit disclosure.**

| Issue | Why B, not A |
|---|---|
| Network-disabled target-laptop run not performed (N2) | Application-level evidence is clean (N1, offline check), but the roadmap's environment condition is unverified. It must be done before relying on air-gapped use |
| `pip-audit` not run | Needs network. The dependency set is pinned and hash-locked, but has no known-vulnerability check |
| Real bank-statement PDF layouts and non-English / handwritten OCR unvalidated | Unrecognised lines are reported, never guessed (W10). Coverage of real statements is unknown |
| No native PDF / DOCX report; HTML printed to PDF is not hashed | Formats are stated in the UI and docs. The JSON / HTML / MD artefacts are hashed (W12) |
| Memos are drafts; no approval / sign-off; single user, no roles | Stated in the UI, reports and docs; sign-off happens outside LedgerSpy |
| Audit truncation detectable only with an external anchor; a local administrator or malware can alter local state | Detection-only design (P20). The anchor procedure works (W13) and depends on the user keeping the anchor |
| No memory cap on the PDF / OCR worker | Limited by pages, pixels and time; a very large file can use much memory |
| Windows x64 + CPython 3.12 only; Tesseract installed separately; not a standalone executable | Stated. Other platforms are unverified |
| SQLite rollback-journal mode: a writer can still wait up to 30 s behind long reads, then gets an explicit "busy" refusal | After D1 a refusal is clean and retryable (no orphan, no open transaction). WAL mode was not adopted in P24 (on-disk change) |
| Evidence files left by an interrupted import with **different** bytes are still refused (`evidence_conflict`) | Deliberate: never trust different content. Recovery is documented (`operations.md` §7) |

**C — Deferred enhancements and optional roadmap items.**
- **Reconciliation:** exceptions do not feed P9; no aging or FX; grouping is contiguous only; references of
  different kinds are not compared; row-level results of stale runs are not kept.
- **Workflow:** case calendar and approval limits by configuration only; master upload in the UI; run / report
  comparison; feedback of decisions into merging and matching.
- **Testing:** coverage measurement; 1M-row performance tests; a unified detector baseline (no P9 recall target);
  detached verification does not survive a reboot.
- **Optional and data-gated phases:** P17 (assistant), P18 (Monte Carlo) and P19 (benchmarking) are optional or
  data-gated and not built; the UI shows "Not available".

## 9. Manual checks still required from the user

1. **Network-disabled install on the target laptop.**
   1. Disable Wi-Fi and Ethernet (or use airplane mode).
   2. Copy the release zip and the Python 3.12 (and, optionally, Tesseract 5) installers from offline media.
   3. Check the zip's SHA-256 and the manifest (`operations.md` §10). Run `install.cmd`, then `doctor.cmd`
      (expect `RESULT: ready`).
   4. Start `ledgerspy.cmd` and run the demo:
      - import `SYNTHETIC_ledger.csv`;
      - import the masters with the documented command (`installation.md` §4);
      - import `SYNTHETIC_bank_statement.csv`, reconcile, open an investigation from a finding, and generate a
        memo and a case report.
   5. Confirm that `audit verify` and `evidence verify` print `OK`. Keep the network off throughout.
2. **`pip-audit`** of `requirements.lock` on a connected build machine. Act on any advisory before release.
3. **One real (anonymised) bank-statement PDF**, if available: check that its lines are parsed or reported as
   unrecognised, and that the totals match the statement.
4. **Tesseract on the target laptop:** `doctor.cmd` shows the `ocr` line ready, if scanned statements are expected.
5. **Record the audit anchor** (`head seq` and `hash`) outside the laptop after each engagement session.

## 10. Final decision

**CONDITIONAL GO** for a limited pilot on Windows x64 / Python 3.12.

- **No known release blocker remains.** The two blockers found by this audit (D1 evidence-store / busy-database
  handling, D2 a misleading report field) are fixed, each with regression tests that fail on the old code.
- **The workflow is correct as tested.** The end-to-end workflow on the installed release candidate passed every
  automated check (30 / 30, plus the manual UI check), with values derived from the source files. Reproduction,
  integrity and tamper detection behaved as documented.
- **Why not GO:**
  - the roadmap's network-disabled-laptop condition (N2) is unverified;
  - `pip-audit` is outstanding;
  - real bank layouts are unvalidated.

  These are environment-dependent checks and disclosed pilot limitations (§8 B, §9), not known defects.

**P24 status:** partially verified. Everything testable on this machine was verified. The disconnected-laptop
acceptance remains the user's step, so the roadmap does not mark P24 done.
